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
import subprocess
import threading
import time as _time
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ai33_tasks, artifacts, cancel, captions, clients, config, render, script, telemetry


# One narration group can produce dozens of tiny FFmpeg encodes. Running
# several groups' decode/slice loops together created 171 FFmpeg launches in
# under a minute for this job, and valid outputs were intermittently rejected.
# Keep paid TTS requests concurrent, but serialize this local disk/FFmpeg
# phase. It is deterministic, cancellation-aware through render._run, and
# avoids turning an I/O scheduling blip into a failed/rebilled job.
_SLICE_LOCK = threading.Lock()


@telemetry.timed("narration_generation")
def generate_voiceover(
    text: str, dest: Path, voice_id: Optional[str] = None,
    with_transcript: bool = True, timeout_s: int = 1800,
) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Synthesize `text`; save MP3 to `dest` and .srt next to it."""
    text = (text or "").strip()
    if not text or not (config.AI33_API_KEY or config.AI33_API_KEY_BACKUP):
        return None, None, None

    request = {
        "text": text,
        "voice_id": voice_id or config.AI33_VOICE_ID,
        "speed": config.AI33_TTS_SPEED,
        "with_transcript": "true" if with_transcript else "false",
    }
    ledger_path = dest.with_suffix(".task.json")
    # A failed custom-voice task is unlike a timeout: AI33 explicitly says no
    # voice was available and the task cannot produce audio.  Clear only that
    # saved failed task and make one new request, which lets the selected
    # primary account (or submit-time account failover) handle it.  Never do
    # this for unknown/network failures, where a paid task may still exist.
    for recovery_attempt in range(2):
        task_id = clients.ai33_submit_form("/v3/text-to-speech", request,
                                           ledger_path=ledger_path)
        try:
            task = clients.ai33_poll(task_id, timeout_s=timeout_s)
            break
        except clients.AI33VoiceNotFound:
            retriable = (recovery_attempt == 0
                         and ai33_tasks.invalidate_definitively_rejected_task(
                             ledger_path, task_id, "elevenlabs_voice_not_found"))
            if not retriable:
                raise
            print("[voiceover] rejected custom voice task will retry on the preferred AI33 account")
    else:  # pragma: no cover - the loop either breaks or raises
        raise RuntimeError("AI33 voice recovery did not produce a task")
    meta = task.get("metadata", {}) or {}

    audio_url = meta.get("audio_url")
    if not audio_url:
        raise RuntimeError(f"ai33 tts: no audio_url in metadata: {str(meta)[:200]}")
    # Never expose a partially downloaded/header-only file at the canonical
    # cache path.  A Stop, network loss, or provider returning the wrong
    # payload must leave the group resumable rather than poisoning every
    # later retry through an existence-only cache hit.
    audio_tmp = dest.with_name(
        f".{dest.stem}.{uuid.uuid4().hex}.download.mp3")
    try:
        clients.download(audio_url, audio_tmp)
        if not render.is_valid_audio(audio_tmp, min_duration=0.25):
            raise RuntimeError(
                "ai33 tts: downloaded payload has no valid audio stream")
        audio_tmp.replace(dest)
    finally:
        audio_tmp.unlink(missing_ok=True)

    srt_path = None
    srt_url = meta.get("srt_url")
    if srt_url:
        srt_path = dest.with_suffix(".srt")
        try:
            clients.download(srt_url, srt_path)
        except cancel.JobCancelled:
            raise
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
        cancel.check()
        group_text = " ".join(beats[i]["text"] for i in idxs)
        contract = {"text": group_text, "voice": voice_id or config.AI33_VOICE_ID,
                    "speed": config.AI33_TTS_SPEED, "transcript": True,
                    "endpoint": "/v3/text-to-speech"}
        slice_contract = {"raw": contract, "indices": idxs,
                          "texts": [beats[i]["text"] for i in idxs], "slicer": 2}
        raw_audio = assets_dir / f"tts_raw_{g:03d}.mp3"
        raw_srt = raw_audio.with_suffix(".srt")
        raw_manifest = raw_audio.with_suffix(".manifest.json")
        slice_manifest = raw_audio.with_suffix(".slices.json")
        beat_audio = [assets_dir / f"vo_{i:03d}.mp3" for i in idxs]
        slice_outputs = [p for audio in beat_audio for p in (audio, audio.with_suffix(".srt")) if p.exists()]
        if (artifacts.matches(slice_manifest, slice_contract, slice_outputs)
                and all(render.is_valid_audio(path) for path in beat_audio)):
            telemetry.count("tts_group_cache_hits")
            for i in idxs:
                dest = assets_dir / f"vo_{i:03d}.mp3"
                srt = dest.with_suffix(".srt")
                beats[i].update(audio=dest, srt=srt if srt.exists() else None,
                                audio_url=None, duration=render.probe_duration(dest))
            continue

        reused_raw = (artifacts.matches(raw_manifest, contract, [raw_audio])
                      and render.is_valid_audio(raw_audio, min_duration=0.25))
        if reused_raw:
            emit(f"Group {g+1}/{total_groups}: reusing existing raw TTS "
                 f"(beats {idxs[0]+1}-{idxs[-1]+1})")
        else:
            if raw_audio.exists():
                emit(f"Group {g+1}/{total_groups}: discarding invalid raw "
                     "TTS cache")
                raw_audio.unlink(missing_ok=True)
                raw_srt.unlink(missing_ok=True)
            elif any(path.exists() for path in beat_audio):
                emit(f"Group {g+1}/{total_groups}: rebuilding invalid "
                     "narration slice(s) from raw TTS")
            for attempt in range(3):
                try:
                    generate_voiceover(group_text, raw_audio, voice_id=voice_id)
                    break
                except (cancel.JobCancelled, clients.AI33VoiceNotFound,
                        ai33_tasks.SubmissionUncertain, ai33_tasks.UsageLimitReached):
                    raise
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    emit(f"Group {g+1}/{total_groups}: TTS retry ({e})")
                    cancel.sleep(8 * (attempt + 1))
        # Validate both newly downloaded and cached raw audio. A truncated
        # response is still a playable MP3, so stream validation alone must
        # not let the next retry bypass this guard and ship missing speech.
        expected_s = len(group_text) / script.CHARS_PER_SEC
        actual_s = render.probe_duration(raw_audio)
        if expected_s > 5 and actual_s < expected_s * 0.5:
            raise RuntimeError(
                f"tts group {g}: got {actual_s:.0f}s audio for "
                f"{len(group_text)} chars (expected ~{expected_s:.0f}s) "
                f"— ai33 may have truncated the request; inspect or replace "
                f"the cached group before retrying")
        if not reused_raw:
            emit(f"Group {g+1}/{total_groups}: narrated "
                 f"({len(idxs)} beats, {len(group_text)} chars, "
                 f"{actual_s/60:.1f} min)")

        artifacts.publish(raw_manifest, contract, [raw_audio])
        _slice_group(beats, idxs, raw_audio, raw_srt, assets_dir)
        for i in idxs:
            fix_srt(beats[i].get("srt"))
        slice_outputs = [p for audio in beat_audio for p in (audio, audio.with_suffix(".srt")) if p.exists()]
        artifacts.publish(slice_manifest, slice_contract, slice_outputs)
        emit(f"Group {g+1}: sliced into beats {idxs[0]+1}-{idxs[-1]+1}")


@telemetry.timed("speech_slicing")
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
    # Submission/polling remains concurrent in the caller. Only locally
    # decoding and writing hundreds of short files is serialized.
    with _SLICE_LOCK:
        try:
            render._run(["ffmpeg", "-y", "-i", str(raw_audio),
                        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(pcm)])
            _slice_group_pcm(beats, idxs, pcm, raw_srt, assets_dir)
        finally:
            # PCM is deliberately an ephemeral decode cache. Keeping it after a
            # failed slice used hundreds of MB per TTS group and made a simple
            # retry consume gigabytes of disk without benefiting resume.
            pcm.unlink(missing_ok=True)


def _slice_group_pcm(beats: List[Dict[str, Any]], idxs: List[int], pcm: Path,
                     raw_srt: Path, assets_dir: Path) -> None:
    """Slice one already-decoded narration master into beat files."""
    cues = captions.parse_srt(raw_srt.read_text(encoding="utf-8", errors="ignore")) \
        if raw_srt.exists() else []
    total_dur = _pcm_wav_duration(pcm) or render.probe_duration(pcm)
    if total_dur <= 0:
        raise RuntimeError(
            f"tts group audio is unprobeable after PCM decode: {pcm.name}")

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

    # Bad provider cue timestamps can map the final beat start to the exact
    # end of the group.  A tiny fixed adjustment technically made the MP3
    # probeable but discarded that beat's speech.  If any mapped segment is
    # implausibly short for its text, fall back to character-proportional
    # boundaries across the known-good full narration instead.
    def _minimum_plausible(i: int) -> float:
        expected = len(beats[i]["text"]) / script.CHARS_PER_SEC
        return min(0.25, max(0.05, expected * 0.10))

    if any(bounds[k + 1] - bounds[k] < _minimum_plausible(i)
           for k, i in enumerate(idxs)):
        bounds = [0.0]
        bounds.extend(
            total_dur * beat_bounds[k][0] / group_norm_len
            for k in range(1, len(idxs)))
        bounds.append(total_dur)

    # Maintain a strict positive interval even for an accidentally empty
    # beat.  The dynamic ceiling makes this feasible for every group length.
    min_seg = min(0.05, total_dur / max(2, len(idxs) * 2))
    for k in range(1, len(bounds) - 1):
        bounds[k] = max(bounds[k], bounds[k - 1] + min_seg)
    for k in range(len(bounds) - 2, 0, -1):
        bounds[k] = min(bounds[k], bounds[k + 1] - min_seg)

    for k, i in enumerate(idxs):
        start, end = bounds[k], bounds[k + 1]
        dest = assets_dir / f"vo_{i:03d}.mp3"
        _write_slice(pcm, dest, start, end, i)
        beat_cues = [captions.Segment(max(0.0, c.start - start),
                                      min(end - start, c.end - start), c.text)
                    for c in cues if c.end > start and c.start < end]
        srt_dest = dest.with_suffix(".srt")
        has_cues = bool(beat_cues)
        if has_cues:
            captions.write_srt(beat_cues, srt_dest)
        beats[i].update(audio=dest, srt=srt_dest if has_cues else None,
                        audio_url=None, duration=end - start)


def _pcm_wav_duration(path: Path) -> float:
    """Read a decoded PCM master's duration without spawning FFprobe.

    FFmpeg has finished and closed this WAV before slicing begins. Reading its
    frame count directly avoids turning a transient FFprobe/process-resource
    failure into a failed job while keeping FFprobe as a fallback for any WAV
    variant the standard-library reader cannot parse.
    """
    try:
        with wave.open(str(path), "rb") as source:
            frame_rate = source.getframerate()
            frames = source.getnframes()
        if frame_rate > 0 and frames > 0:
            return frames / frame_rate
    except (EOFError, OSError, wave.Error):
        pass
    return 0.0


def _write_slice(pcm: Path, dest: Path, start: float, end: float, beat_index: int) -> None:
    """Encode and verify a local beat slice without ever exposing a partial file.

    ``-t`` expresses an output *duration*. The previous ``-to`` position was
    needlessly ambiguous when combined with input seeking. A filesystem or
    FFprobe hiccup after FFmpeg exits is retried once locally; it never causes
    a new AI33 request because this operates entirely on the retained raw TTS.
    """
    duration = max(0.001, end - start)
    min_valid = min(0.25, max(0.04, duration * 0.25))
    for attempt in range(2):
        tmp = dest.with_name(f".{dest.stem}.{uuid.uuid4().hex}.slice.mp3")
        try:
            render._run(["ffmpeg", "-y", "-ss", f"{start:.3f}",
                        "-i", str(pcm), "-t", f"{duration:.3f}",
                        "-c:a", "libmp3lame", "-q:a", "2", str(tmp)])
            if _slice_audio_is_valid(tmp, min_valid):
                tmp.replace(dest)
                return
        finally:
            tmp.unlink(missing_ok=True)
    raise RuntimeError(
        f"tts beat {beat_index}: sliced audio remained invalid after local retry "
        f"({start:.3f}s-{end:.3f}s)")


def _slice_audio_is_valid(path: Path, min_duration: float) -> bool:
    """Validate the newly written MP3 with independent metadata and decode paths."""
    if render.is_valid_audio(path, min_duration=min_duration):
        return True
    # On the failing job, FFprobe intermittently reported no usable stream
    # even though this exact saved segment was then probeable and fully
    # decodable. A fresh decode is a stronger check than reusing that one
    # metadata answer; it never accepts a header-only or corrupt MP3.
    try:
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
             "-map", "0:a:0", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=30)
        return decoded.returncode == 0 and path.stat().st_size > 256
    except (OSError, subprocess.SubprocessError):
        return False
