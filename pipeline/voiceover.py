"""Voiceover + subtitles via ai33.pro /v3/text-to-speech.

with_transcript=true makes ai33 return a ready-made .srt subtitle file
(metadata.srt_url) alongside the audio, which we burn into the video.

Returns (audio_path, srt_path, audio_url). The remote audio_url is kept
because kie's avatar models take the narration by URL — chaining the two
providers without re-uploading. srt_path/audio_url may be None.

Narration is voiced in as few large continuous passes as possible instead
of one call per beat (see group_beats_for_tts/generate_voiceover_grouped
below) — synthesizing each ~15-40s beat in isolation lets the model reset
pitch/prosody at every single chunk boundary; a handful of few-minute
passes has far fewer such boundaries in the finished narration. Tested
empirically against the real API at up to 40,054 chars (~40 min) with zero
truncation — see AI33_MAX_TTS_CHARS in config.py.
"""
from __future__ import annotations

import bisect
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import captions, clients, config, render, script


def generate_voiceover(
    text: str, dest: Path, voice_id: Optional[str] = None,
    with_transcript: bool = True, timeout_s: int = 1800,
) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Synthesize `text`; save MP3 to `dest` and .srt next to it."""
    text = (text or "").strip()
    if not text or not (config.AI33_API_KEY or config.AI33_API_KEY_BACKUP):
        return None, None, None

    task_id = clients.ai33_submit_form("/v3/text-to-speech", {
        "text": text,
        "voice_id": voice_id or config.AI33_VOICE_ID,
        "speed": config.AI33_TTS_SPEED,
        "with_transcript": "true" if with_transcript else "false",
    })
    task = clients.ai33_poll(task_id, timeout_s=timeout_s)
    meta = task.get("metadata", {}) or {}

    audio_url = meta.get("audio_url")
    if not audio_url:
        raise RuntimeError(f"ai33 tts: no audio_url in metadata: {str(meta)[:200]}")
    clients.download(audio_url, dest)

    srt_path = None
    srt_url = meta.get("srt_url")
    if srt_url:
        srt_path = dest.with_suffix(".srt")
        try:
            clients.download(srt_url, srt_path)
        except Exception as e:  # noqa: BLE001
            print(f"[voiceover] srt download failed: {e}")
            srt_path = None
    return dest, srt_path, audio_url


# ─────────────────────── grouped (few large calls) TTS ───────────────────
def group_beats_for_tts(beats: List[Dict[str, Any]], max_chars: int) -> List[List[int]]:
    """Batch consecutive beat indices into the fewest groups whose combined
    text stays under max_chars. A beat longer than max_chars on its own
    still becomes its own (oversized) group — script.py's own ~1.6x-target
    cap on beat size means this shouldn't happen in practice, but it's
    handled rather than assumed impossible."""
    groups: List[List[int]] = []
    cur: List[int] = []
    cur_len = 0
    for i, b in enumerate(beats):
        t_len = len(b["text"])
        added = t_len + (1 if cur else 0)  # +1 for the joining space
        if cur and cur_len + added > max_chars:
            groups.append(cur)
            cur, cur_len = [], 0
            added = t_len
        cur.append(i)
        cur_len += added
    if cur:
        groups.append(cur)
    return groups


def generate_voiceover_grouped(
    beats: List[Dict[str, Any]], groups: List[Tuple[int, List[int]]],
    assets_dir: Path,
    voice_id: Optional[str] = None,
    emit: Callable[[str], None] = lambda m: None,
    fix_srt: Callable[[Optional[Path]], None] = lambda p: None,
    total_groups: Optional[int] = None,
) -> None:
    """Replace per-beat TTS with grouped large-chunk TTS, then slice each
    group's single audio+srt back into per-beat vo_{i:03d}.mp3/.srt files.

    `groups` is a list of (global_group_index, beat_indices) pairs — the
    index MUST be the group's absolute position in the full tts_groups
    list, not re-derived locally. When groups are processed concurrently
    by calling this function once per group (one-element `groups` list per
    call, as orchestrator.py does for wall-clock parallelism), a locally
    re-derived index would restart at 0 on every call, making concurrent
    groups collide on the same tts_raw_000.* filename — exactly the kind
    of cross-job race already fixed elsewhere in this codebase (queue
    persistence, the particle-overlay cache), caught here by a real
    reproduction: two concurrent groups sharing one raw-audio file
    corrupted each other's slicing into zero-duration cuts.

    Mutates each beat dict in place: audio, srt, audio_url (always None —
    HeyGen already falls back to uploading local audio when this is
    absent), duration. Resumable at two levels: the raw per-group TTS
    output is cached (tts_raw_{g:03d}.*), and a group whose every per-beat
    output file already exists on disk is skipped entirely.
    """
    total_groups = total_groups or len(groups)
    for g, idxs in groups:
        if all((assets_dir / f"vo_{i:03d}.mp3").exists() for i in idxs):
            for i in idxs:
                dest = assets_dir / f"vo_{i:03d}.mp3"
                srt = dest.with_suffix(".srt")
                beats[i].update(audio=dest, srt=srt if srt.exists() else None,
                                audio_url=None, duration=render.probe_duration(dest))
            continue

        group_text = " ".join(beats[i]["text"] for i in idxs)
        raw_audio = assets_dir / f"tts_raw_{g:03d}.mp3"
        raw_srt = raw_audio.with_suffix(".srt")
        if raw_audio.exists():
            emit(f"Group {g+1}/{total_groups}: reusing existing raw TTS "
                 f"(beats {idxs[0]+1}-{idxs[-1]+1})")
        else:
            for attempt in range(3):  # a flaky group must not kill the whole run
                try:
                    generate_voiceover(group_text, raw_audio, voice_id=voice_id)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    emit(f"Group {g+1}/{total_groups}: TTS retry ({e})")
                    _time.sleep(8 * (attempt + 1))
            # Truncation safety net: even though 40k chars tested clean, a
            # future provider change (or a raised AI33_MAX_TTS_CHARS) could
            # hit a real ceiling — fail loudly rather than silently ship
            # missing narration.
            expected_s = len(group_text) / script.CHARS_PER_SEC
            actual_s = render.probe_duration(raw_audio)
            if expected_s > 5 and actual_s < expected_s * 0.5:
                raise RuntimeError(
                    f"tts group {g}: got {actual_s:.0f}s audio for "
                    f"{len(group_text)} chars (expected ~{expected_s:.0f}s) "
                    f"— ai33 may have truncated the request; lower "
                    f"AI33_MAX_TTS_CHARS in .env and resume")
            emit(f"Group {g+1}/{total_groups}: narrated "
                 f"({len(idxs)} beats, {len(group_text)} chars, "
                 f"{actual_s/60:.1f} min)")

        _slice_group(beats, idxs, raw_audio, raw_srt, assets_dir)
        for i in idxs:
            fix_srt(beats[i].get("srt"))
        emit(f"Group {g+1}: sliced into beats {idxs[0]+1}-{idxs[-1]+1}")


def _slice_group(beats: List[Dict[str, Any]], idxs: List[int], raw_audio: Path,
                 raw_srt: Path, assets_dir: Path) -> None:
    """Cut one group's single audio+transcript into each beat's own
    vo_{i:03d}.mp3/.srt, mutating `beats` in place."""
    # Decode to PCM once rather than stream-copy-cutting the compressed
    # MP3 directly — MP3's bit-reservoir means a cut at an arbitrary
    # (non-frame-boundary) timestamp on the compressed stream can produce
    # an audible click right at the cut. Cutting decoded PCM is
    # sample-accurate; re-encoding each short segment back to mp3 is cheap.
    pcm = raw_audio.with_suffix(".wav")
    render._run(["ffmpeg", "-y", "-i", str(raw_audio),
                "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(pcm)])
    cues = captions.parse_srt(raw_srt.read_text(encoding="utf-8", errors="ignore")) \
        if raw_srt.exists() else []
    total_dur = render.probe_duration(pcm)

    # Beat-side offsets: cumulative length of each beat's OWN text, joined
    # with a single space — the exact same normalization used to build
    # group_text in generate_voiceover_grouped, so this is an exact match,
    # not an approximation.
    beat_bounds = []
    acc = 0
    for i in idxs:
        t = beats[i]["text"]
        beat_bounds.append((acc, acc + len(t)))
        acc += len(t) + 1
    group_norm_len = max(1, acc - 1)

    # Cue-side offsets, same normalization, from the returned transcript.
    cue_bounds = []
    acc2 = 0
    for c in cues:
        cue_bounds.append((acc2, acc2 + len(c.text), c))
        acc2 += len(c.text) + 1
    cue_norm_len = acc2 - 1 if cues else 0

    # Piecewise-linear char-offset -> time breakpoints, anchored at every
    # cue's start AND end (not just containment ranges) — gaps between
    # cues (pauses, silence) get interpolated too. A char_pos landing in a
    # gap must NOT be treated as "no cue contains this", which is what a
    # containment search does; that previously made any offset inside an
    # inter-cue gap silently snap to the group's final cue end (i.e. the
    # end of the whole group), collapsing every following beat to zero
    # length. Breakpoints always bracket every position in range, so this
    # can't happen.
    breakpoints: List[Tuple[float, float]] = []
    for cs, ce, c in cue_bounds:
        breakpoints.append((cs, c.start))
        breakpoints.append((ce, c.end))
    bp_xs = [bp[0] for bp in breakpoints]

    def _time_at(char_pos: int) -> float:
        if not breakpoints or group_norm_len == 0:
            # No SRT (download failed) — fall back to a flat speech-rate
            # assumption across the whole group's audio: degraded
            # per-beat split accuracy, but never a missing/blank beat.
            return total_dur * char_pos / group_norm_len
        # Rescale for any drift between the two normalizations (the
        # provider may re-punctuate/re-space slightly) instead of letting
        # small per-cue drift compound over a long group.
        scaled = char_pos * (cue_norm_len / group_norm_len)
        idx = bisect.bisect_right(bp_xs, scaled) - 1
        idx = max(0, min(idx, len(breakpoints) - 2))
        x0, t0 = breakpoints[idx]
        x1, t1 = breakpoints[idx + 1]
        if x1 <= x0:
            return t0
        frac = (scaled - x0) / (x1 - x0)
        return t0 + frac * (t1 - t0)

    bounds = [0.0]
    for k in range(1, len(idxs)):
        t = _time_at(beat_bounds[k][0])
        bounds.append(max(bounds[-1], min(total_dur, t)))
    bounds.append(total_dur)

    # _time_at can clip an intermediate boundary to exactly total_dur (an
    # offset past the last SRT cue), which combined with the unconditional
    # final total_dur bound gives ffmpeg an inverted/zero-length -ss/-to
    # range on the last beat and crashes the whole group. Walk backward and
    # pull any colliding boundary down just enough to keep every segment a
    # non-zero length.
    MIN_SEG = 0.05
    for k in range(len(bounds) - 1, 0, -1):
        if bounds[k] - bounds[k - 1] < MIN_SEG:
            bounds[k - 1] = max(0.0, bounds[k] - MIN_SEG)

    for k, i in enumerate(idxs):
        start, end = bounds[k], bounds[k + 1]
        dest = assets_dir / f"vo_{i:03d}.mp3"
        render._run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                    "-i", str(pcm), "-c:a", "libmp3lame", "-q:a", "2", str(dest)])
        beat_cues = [captions.Segment(max(0.0, c.start - start),
                                      min(end - start, c.end - start), c.text)
                    for c in cues if c.end > start and c.start < end]
        srt_dest = dest.with_suffix(".srt")
        has_cues = bool(beat_cues)
        if has_cues:
            captions.write_srt(beat_cues, srt_dest)
        beats[i].update(audio=dest, srt=srt_dest if has_cues else None,
                        audio_url=None, duration=end - start)
    pcm.unlink(missing_ok=True)
