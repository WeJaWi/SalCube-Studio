"""End-to-end Grandma Hannah pipeline.

brief → outline + 4 hooks → APPROVAL GATE → full script → typed beats →
host placement (HeyGen budget) → TTS → parallel media → FFmpeg render.

The gate is a callback: the app shows the hooks/outline in the UI, the user
picks one hook and approves (or edits) the outline, and the pipeline resumes.
Passing auto_hook skips the gate (used for tests / hands-off runs).
"""
from __future__ import annotations

import json
import math
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (animation, aivideo, cancel, config, footage, heygen, images,
               llm, music, render, script, voiceover)

ProgressFn = Callable[[str, str], None]
GateFn = Callable[[Dict[str, str], str], Tuple[str, str]]


def load_profile(name: str) -> Dict[str, Any]:
    path = config.PROFILES_DIR / name
    if not path.suffix:
        path = path.with_suffix(".json")
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")
    return json.loads(path.read_text())


def _track_count(length_chars: int, music_cfg: Dict[str, Any]) -> int:
    setting = music_cfg.get("tracks", "auto")
    if isinstance(setting, int) and setting > 0:
        return setting
    est_minutes = length_chars / 1000.0
    per = float(music_cfg.get("minutes_per_track", 20))
    lo = int(music_cfg.get("min_tracks", 1))
    hi = int(music_cfg.get("max_tracks", 4))
    return max(lo, min(hi, math.ceil(est_minutes / max(1.0, per))))


def _check_disk(emit, need_gb: float = 2.0, warn_gb: float = 6.0) -> None:
    """Abort early with a clear message instead of dying mid-render.

    Measures the volume `output/` actually lives on, not "/" — those differ
    on Windows (where "/" isn't even the right drive) and on any Mac whose
    output dir sits on an external or secondary volume, in which case the
    old check happily reported the boot disk's free space instead.
    """
    free_gb = shutil.disk_usage(config.OUTPUT_DIR).free / 1e9
    if free_gb < need_gb:
        raise RuntimeError(
            f"only {free_gb:.1f} GB of disk space free — free some space and "
            f"resume this job; nothing generated so far is lost")
    if free_gb < warn_gb:
        emit("start", f"⚠ low disk space ({free_gb:.1f} GB free) — the render "
                      f"may not fit; consider freeing space first")


def _host_still(profile: Dict[str, Any], profile_name: str) -> Optional[Path]:
    """A still of the host, used when HeyGen is unavailable or a clip fails.
    Drop one at hosts/<profile>.png (or set host.image in the profile)."""
    custom = (profile.get("host", {}).get("image") or "").strip()
    if custom:
        p = Path(custom) if custom.startswith("/") else config.ROOT / custom
        if p.exists():
            return p
    p = config.HOSTS_DIR / f"{profile_name}.png"
    return p if p.exists() else None


def _broll_fallback(b: Dict[str, Any], i: int, assets: Path, width: int,
                    height: int, emit, skip_video: bool = False,
                    used: Optional[Dict[str, int]] = None, smart: str = "off",
                    max_repeats: int = 2) -> None:
    """Fallback chain: stock video → stock image → AI image → blank."""
    query = b.get("query") or " ".join(b["text"].split()[:3])
    if not skip_video:
        media = footage.fetch_stock(query, assets / f"beat_{i:03d}_fb.mp4",
                                    used=used, narration=b["text"], smart=smart,
                                    max_repeats=max_repeats)
        if media:
            b["media"], b["media_kind"] = media, "stock"
            return
    media = footage.fetch_stock_image(query, assets / f"beat_{i:03d}_fb.jpg",
                                      used=used, narration=b["text"],
                                      smart=smart, max_repeats=max_repeats)
    if media:
        b["media"], b["media_kind"] = media, "image"
        return
    try:
        emit("broll", f"Beat {i+1}: fallback AI image")
        b["media"] = images.generate_image(
            f"{b['text'][:140]}, warm documentary photo",
            assets / f"beat_{i:03d}_fb.png", width, height)
        b["media_kind"] = "image"
    except Exception as e:  # noqa: BLE001
        emit("broll", f"Beat {i+1}: image fallback failed ({e}) — blank card")
        b["media"], b["media_kind"] = None, "blank"


def run_pipeline(inputs: Dict[str, Any], profile_name: str, job_dir: Path,
                 progress: Optional[ProgressFn] = None,
                 gate: Optional[GateFn] = None,
                 voice_gate: Optional[Callable[[Dict[str, Any]], None]] = None,
                 beat_progress: Optional[Callable[[int, int], None]] = None,
                 on_planned: Optional[Callable[[Dict[str, Any]], None]] = None,
                 profile_data: Optional[Dict[str, Any]] = None,
                 ) -> Path:
    def emit(step: str, msg: str) -> None:
        # Every stage boundary and progress line funnels through here, so
        # one check gives cancellation coverage across the whole pipeline
        # (ffmpeg is killed separately by the token — see pipeline/cancel.py).
        cancel.check()
        print(f"[{step}] {msg}")
        if progress:
            progress(step, msg)

    job_dir.mkdir(parents=True, exist_ok=True)
    assets = job_dir / "assets"
    assets.mkdir(exist_ok=True)

    render.ensure_ffmpeg()
    _check_disk(emit)
    # Fleet dispatch includes a snapshot so a profile edit on the main takes
    # effect remotely without requiring a perfectly synchronized checkout.
    profile = profile_data if isinstance(profile_data, dict) else load_profile(profile_name)
    d = profile.get("defaults", {})
    v = profile.get("video", {})
    width, height = int(v.get("width", 1920)), int(v.get("height", 1080))
    fps = int(v.get("fps", 30))

    broll = inputs.get("broll") or ["stock", "stock_image", "ai_video", "animation"]
    inputs = {
        "title": inputs.get("title", "").strip(),
        "language": (inputs.get("language") or d.get("language", "English")).strip(),
        "length_chars": int(inputs.get("length_chars") or d.get("length_chars", 15000)),
        "comment_cta": bool(inputs.get("comment_cta", True)),
        "promote_ebook": bool(inputs.get("promote_ebook", False)),
        "ebook_cta_at": int(inputs.get("ebook_cta_at") or 0) or None,
        "promote_community": bool(inputs.get("promote_community", False)),
        "auto_hook": (inputs.get("auto_hook") or "").strip() or None,
        "script_model": (inputs.get("script_model") or "").strip() or None,
        "patreon_split": bool(inputs.get("patreon_split", False)),
        "patreon_cut_from": inputs.get("patreon_cut_from"),
        "patreon_cut_to": inputs.get("patreon_cut_to"),
        "cited_philosophers": (inputs.get("cited_philosophers") or "").strip(),
        "custom_script": (inputs.get("custom_script") or "").strip(),
        "ebook_text": (inputs.get("ebook_text") or "").strip(),
        "broll": broll,
        "mindmap": bool(inputs.get("mindmap", False)),
        "anim_templates": inputs.get("anim_templates"),
        "anim_themes": [t for t in (inputs.get("anim_themes")
                                    or v.get("anim_themes_default")
                                    or ["classic"])
                        if t in animation.THEMES] or ["classic"],
        "smart_stock": (inputs.get("smart_stock")
                        or v.get("smart_stock") or "text").strip().lower(),
        "qr_badge": bool(inputs.get("qr_badge", False)),
        "spotify_popup": bool(inputs.get("spotify_popup", False)),
        "spotify_url": (inputs.get("spotify_url") or "").strip(),
    }
    if inputs["smart_stock"] not in ("vision", "text", "off"):
        inputs["smart_stock"] = "text"
    smart = inputs["smart_stock"]
    model = llm.resolve_model(inputs.get("script_model"))
    # Read once per job — like `model` above, never live off a mutable
    # global, so a settings toggle flipped mid-render can't affect a job
    # already in flight (only jobs started after the flip pick it up).
    gpu_encode = bool(config.load_settings().get("gpu_encode", False))
    beat_workers = config.resolve_beat_workers(gpu_encode)
    emit("start", f"“{inputs['title']}” · {inputs['language']} · "
                  f"{inputs['length_chars']} chars · script model: {model} · "
                  f"b-roll: {', '.join(broll)} · stock matching: {smart} · "
                  f"encoder: {'VideoToolbox (GPU)' if gpu_encode else 'x264 (CPU)'} "
                  f"· {beat_workers} beat worker(s)"
                  + (" · ebook CTA" if inputs["promote_ebook"] else ""))

    # 1+2. Outline, gate, and full script — skipped when the job dir already
    # has a script.txt (crash resume: the same dir is re-run and every asset
    # already on disk is reused instead of re-billed). If outline/hooks exist
    # but the script died mid-write (e.g. provider 500), reuse them and only
    # re-run write_script — no second outline bill.
    script_file = job_dir / "script.txt"
    outline_file = job_dir / "outline.txt"
    hooks_file = job_dir / "hooks.json"
    if script_file.exists():
        narration = script_file.read_text()
        emit("script", f"Resuming: reusing existing script ({len(narration)} chars)")
    elif (inputs.get("custom_script") or "").strip():
        narration = inputs["custom_script"].strip()
        script_file.write_text(narration)
        emit("script", f"Using your provided script ({len(narration)} chars) — "
                       f"skipping outline, hooks, and the approval gate")
    else:
        if outline_file.exists() and hooks_file.exists():
            outline = outline_file.read_text()
            hooks = json.loads(hooks_file.read_text())
            emit("outline", f"Resuming: reusing outline ({len(outline)} chars) "
                            f"+ {len(hooks)} hooks from disk")
            if inputs["auto_hook"]:
                chosen = (inputs["auto_hook"] if inputs["auto_hook"] in hooks
                          else next(iter(hooks)))
                hook_text = hooks[chosen]
                emit("outline", f"auto-approved with the {chosen} hook")
            elif gate:
                emit("awaiting", "Waiting for your hook choice + outline approval…")
                hook_text, outline = gate(hooks, outline)
                emit("outline", "Approved — writing the full script")
            else:
                hook_text = next(iter(hooks.values()))
        else:
            emit("outline", f"Writing detailed outline + hooks ({model})…")
            stage1 = script.outline_and_hooks(inputs, profile, model,
                                              log=lambda m: emit("outline", m))
            outline, hooks = stage1["outline"], stage1["hooks"]
            outline_file.write_text(outline)
            hooks_file.write_text(json.dumps(hooks, indent=2))

            if inputs["auto_hook"]:
                chosen = (inputs["auto_hook"] if inputs["auto_hook"] in hooks
                          else next(iter(hooks)))
                hook_text = hooks[chosen]
                emit("outline", f"auto-approved with the {chosen} hook")
            elif gate:
                emit("awaiting", "Waiting for your hook choice + outline approval…")
                hook_text, outline = gate(hooks, outline)
                emit("outline", "Approved — writing the full script")
            else:
                hook_text = next(iter(hooks.values()))

        emit("script", "Writing the full script…")
        narration = script.write_script(inputs, profile, outline, hook_text,
                                        model, log=lambda m: emit("script", m))
        if inputs["promote_ebook"] and inputs.get("ebook_cta_at"):
            emit("script", f"Weaving e-book CTA near "
                           f"{inputs['ebook_cta_at']} chars…")
            try:
                narration = script.insert_ebook_cta(
                    narration, inputs, profile, inputs["ebook_cta_at"], model,
                    log=lambda m: emit("script", m))
            except Exception as e:  # noqa: BLE001
                emit("script", f"⚠ e-book CTA weave failed ({e}) — this "
                               f"video has NO e-book CTA")
        if inputs["patreon_split"]:
            lo = int(inputs.get("patreon_cut_from") or 0)
            hi = int(inputs.get("patreon_cut_to") or 0)
            pos = (lo + hi) // 2 if lo and hi else (lo or hi or len(narration) // 2)
            emit("script", f"Inserting Patreon cut near {pos} chars…")
            try:
                narration = script.insert_patreon_cut(
                    narration, pos, inputs["language"], model,
                    log=lambda m: emit("script", m))
            except Exception as e:  # noqa: BLE001
                emit("script", f"Patreon cut insertion failed ({e}) — "
                               f"inserting plain marker fallback")
                narration = script.insert_patreon_cut_fallback(
                    narration, pos, inputs["language"],
                    log=lambda m: emit("script", m))
        script_file.write_text(narration)
        emit("script", f"Narration ready ({len(narration)} chars)")

    # Resume path: script on disk may predate a failed cut (e.g. missing
    # json_repair). If Patreon is on and the marker is absent, insert now.
    if (inputs.get("patreon_split")
            and not re.search(r"\[——[^\]\n]*——\]", narration)):
        lo = int(inputs.get("patreon_cut_from") or 0)
        hi = int(inputs.get("patreon_cut_to") or 0)
        pos = (lo + hi) // 2 if lo and hi else (lo or hi or len(narration) // 2)
        emit("script", f"Patreon marker missing on resume — inserting near "
                       f"{pos} chars…")
        try:
            narration = script.insert_patreon_cut(
                narration, pos, inputs["language"], model,
                log=lambda m: emit("script", m))
        except Exception as e:  # noqa: BLE001
            emit("script", f"Patreon cut rewrite failed ({e}) — plain marker")
            narration = script.insert_patreon_cut_fallback(
                narration, pos, inputs["language"],
                log=lambda m: emit("script", m))
        script_file.write_text(narration)
        # Beat plan / VO indices no longer match the rewritten script.
        plan_file = job_dir / "plan.json"
        if plan_file.exists():
            plan_file.unlink()
            emit("plan", "Cleared beat plan after Patreon cut (will replan)")
        assets = job_dir / "assets"
        if assets.is_dir():
            cleared = 0
            for pat in ("vo_*.mp3", "vo_*.srt", "vo_*.ass", "host_*.mp4",
                        "beat_*.mp4", "beat_*.jpg", "beat_*.png"):
                for p in assets.glob(pat):
                    p.unlink(missing_ok=True)
                    cleared += 1
            if cleared:
                emit("plan", f"Cleared {cleared} beat/VO assets for replan")
        for p in job_dir.glob("beat_*.mp4"):
            p.unlink(missing_ok=True)
        for name in ("final.mp4", "final_part1.mp4", "final_part2.mp4",
                     "patreon_cut.json", "patreon_cut.txt"):
            (job_dir / name).unlink(missing_ok=True)

    # Patreon split: the writer places a marker line that must never be
    # spoken — strip it, remember where it fell, report the cut point later.
    cut_offset = None
    m = re.search(r"\[——[^\]\n]*——\]", narration)
    if m:
        cut_offset = m.start()
        narration = narration[:m.start()] + narration[m.end():]

    # Narration is now final (post-CTA-weave, post-Patreon-rewrite, marker
    # stripped) and nothing but LLM calls have been billed so far — this is
    # the true character count, and the natural boundary between the
    # LLM-bound planning work above and the beat-planning/TTS/render work
    # below. Purely informational in this increment (never blocks); a
    # future increment upgrades this into a real dispatch gate at the same
    # call site.
    if on_planned:
        try:
            beat_seconds = float(v.get("beat_seconds", 15))
            chars_per_beat = max(120, int(1000 * beat_seconds / 60))
            on_planned({
                "chars": len(narration),
                "est_beats": max(3, round(len(narration) / chars_per_beat)),
            })
        except Exception:  # noqa: BLE001
            pass  # informational only — must never break the render

    # 3. Plan b-roll beats, then place the host inside the credit budget.
    # The plan is persisted so a resumed run works with identical beats.
    plan_file = job_dir / "plan.json"
    mindmap = None
    if plan_file.exists():
        saved = json.loads(plan_file.read_text())
        beats, music_prompt = saved["beats"], saved["music_prompt"]
        mindmap = saved.get("mindmap")
        # back-compat: a plan.json from before grouped TTS existed lacks
        # tts_groups — compute it fresh in that one case only (there is
        # nothing to preserve since no grouped TTS ever ran against it yet)
        tts_groups = saved.get("tts_groups") or voiceover.group_beats_for_tts(
            beats, config.AI33_MAX_TTS_CHARS)
        emit("plan", f"Resuming: reusing existing beat plan ({len(beats)} beats)")
    else:
        emit("plan", "Planning b-roll beats…")
        beats, music_prompt = script.plan_beats(
            narration, profile, broll, model,
            anim_templates=inputs["anim_templates"],
            language=inputs["language"], log=lambda m: emit("plan", m))
        beats = script.mark_host_segments(beats, profile,
                                          log=lambda m: emit("plan", m))
        if inputs["mindmap"]:
            emit("plan", "Building the mind-map slide…")
            mindmap = script.plan_mindmap(inputs, "", beats, model,
                                          log=lambda m: emit("plan", m))
        # Grouping is computed once and persisted (like beats/music_prompt)
        # so a later change to AI33_MAX_TTS_CHARS can't make a resume
        # recompute different group boundaries than what was already
        # partially voiced — resume must always match the original plan.
        tts_groups = voiceover.group_beats_for_tts(beats, config.AI33_MAX_TTS_CHARS)
        plan_file.write_text(json.dumps(
            {"beats": beats, "music_prompt": music_prompt,
             "mindmap": mindmap, "tts_groups": tts_groups}, indent=1))
    # JSON round-trips dict keys as strings — normalize placement indices
    if mindmap and mindmap.get("placements"):
        mindmap["placements"] = {int(k): int(v)
                                 for k, v in mindmap["placements"].items()}
    counts: Dict[str, int] = {}
    for b in beats:
        key = "split" if b.get("split") else b["type"]
        counts[key] = counts.get(key, 0) + 1
    emit("plan", f"{len(beats)} beats: " + ", ".join(f"{n} {t}" for t, n in counts.items()))

    # 4. Music early (parallel with everything below). A resumed job reuses
    # its stitched bed instead of paying for new Suno tracks.
    music_tasks: List[str] = []
    music_cfg = profile.get("music", {})
    if (assets / "music.mp3").exists():
        emit("music", "Resuming: reusing existing music bed")
    elif music_cfg.get("enabled", True):
        n_tracks = _track_count(inputs["length_chars"], music_cfg)
        emit("music", f"Starting {n_tracks} music tracks (Suno)…")
        music_tasks = music.submit_music(music_prompt, n_tracks)

    # 5. Per-beat TTS (the full narration is voiced here — HeyGen only ever
    # lip-syncs segments of this same audio, so the voice never changes)
    voices = profile.get("voices", {}) or {}
    lang = (inputs.get("language") or "").strip()
    # Case-insensitive match: UI often stores "german" while profiles key
    # "German". Without this we silently fall back to AI33_VOICE_ID and the
    # channel's custom clone (e.g. Matthias clone_2616153) is never used.
    voice_id = None
    if lang:
        voice_id = voices.get(lang) or voices.get(lang.split()[0])
        if not voice_id:
            lang_l = lang.lower()
            # IMPORTANT: do not use loop var name `v` — it shadows the
            # video-config dict `v = profile.get("video")` used later for
            # transitions/patreon timing (AttributeError: str has no .get).
            for vk, vv in voices.items():
                if isinstance(vk, str) and vk.lower() == lang_l:
                    voice_id = vv
                    break
            if not voice_id:
                for vk, vv in voices.items():
                    if isinstance(vk, str) and vk.lower().startswith(lang_l):
                        voice_id = vv
                        break
    emit("voiceover", f"Using voice {voice_id or config.AI33_VOICE_ID} "
                      f"(lang={lang or 'default'})")

    # Burned-in caption fixes (e.g. the transcriber misspells the shop domain
    # — a viewer typing "grandmahanna.store" hits nothing). Applied to every
    # beat SRT, fresh or resumed.
    sub_fixes = profile.get("subtitle_fixes") or []
    def _fix_srt(path) -> None:
        if not (path and Path(path).exists() and sub_fixes):
            return
        txt = orig = Path(path).read_text()
        for f in sub_fixes:
            txt = re.sub(f["pattern"], f["replace"], txt)
        if txt != orig:
            Path(path).write_text(txt)

    # Narration is voiced in as few large continuous passes as possible
    # (tts_groups, computed above) instead of one call per beat — far fewer
    # audible prosody-reset boundaries in the finished narration. Each
    # group's single audio+transcript is sliced locally back into the
    # per-beat vo_{i:03d}.mp3/.srt files render.py/heygen.py already expect
    # unchanged (see voiceover.generate_voiceover_grouped). A handful of
    # groups still run concurrently for wall-clock on very long (3h)
    # videos — this only affects wall-clock, not the number of audible
    # boundaries (a boundary between two groups exists either way).
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(tts_groups)))) as pool:
        def _one_group(g_idxs: Tuple[int, List[int]]) -> None:
            # Pass the GLOBAL group index explicitly (not re-derived via a
            # local enumerate()) — concurrent single-group calls must never
            # collide on the same tts_raw_000.* filename.
            voiceover.generate_voiceover_grouped(
                beats, [g_idxs], assets, voice_id=voice_id,
                emit=lambda m: emit("voiceover", m), fix_srt=_fix_srt,
                total_groups=len(tts_groups))
        # Slicing a group calls render._run (ffmpeg), so pool workers need
        # the job's cancel token too — same reason as the beat pool.
        _tok = cancel.current()
        list(pool.map(cancel.adopt(_tok, _one_group), enumerate(tts_groups)))

    # Patreon cut: map char offset → beat index; persist for resume + split render
    cut_beat: Optional[int] = None  # 0-based index of LAST free-half beat
    if cut_offset is not None:
        acc, cut_beat = 0, len(beats) - 1
        for i, b in enumerate(beats):
            acc += len(b["text"]) + 1
            if acc >= cut_offset:
                cut_beat = i
                break
        # Never leave the paid half empty
        if cut_beat >= len(beats) - 1:
            cut_beat = max(0, len(beats) - 2)
        tr_cfg = v.get("transitions", {}) or {}
        tr = float(tr_cfg.get("seconds", 0.5)) if tr_cfg.get("enabled", True) else 0.0
        t = sum(float(bb.get("duration", 0)) for bb in beats[:cut_beat + 1]) + cut_beat * tr
        stamp = f"{int(t // 60)}m{int(t % 60):02d}s"
        cut_info = {
            "cut_beat": cut_beat + 1,  # 1-based for humans
            "cut_beat_index": cut_beat,  # 0-based for render
            "cut_time": stamp,
            "total_beats": len(beats),
            "free_beats": cut_beat + 1,
            "paid_beats": len(beats) - (cut_beat + 1),
        }
        (job_dir / "patreon_cut.txt").write_text(
            f"Patreon cut: after beat {cut_beat + 1} of {len(beats)}, ≈ {stamp}\n"
            f"Will render TWO videos:\n"
            f"  final_part1.mp4  — free half (beats 1–{cut_beat + 1})\n"
            f"  final_part2.mp4  — paid half (beats {cut_beat + 2}–{len(beats)})\n")
        (job_dir / "patreon_cut.json").write_text(
            json.dumps(cut_info, indent=1))
        emit("plan", f"Patreon cut after beat {cut_beat + 1} (≈ {stamp}) — "
                     f"will render free half then paid half")
        if voice_gate:
            total = (sum(float(bb.get("duration", 0)) for bb in beats)
                     + max(0, len(beats) - 1) * tr)
            total_stamp = f"{int(total // 60)}m{int(total % 60):02d}s"
            emit("plan", f"Voice ready: total ≈ {total_stamp}, Patreon "
                         f"cut ≈ {stamp} (part1 = beats 1–{cut_beat + 1}) — "
                         f"auto-continuing to visuals")
            voice_gate({"cut_time": stamp, "cut_beat": cut_beat + 1,
                        "total_time": total_stamp, "beats": len(beats),
                        "free_beats": cut_beat + 1,
                        "paid_beats": len(beats) - (cut_beat + 1)})
            emit("plan", "Voice auto-approved — generating visuals")
    elif inputs.get("patreon_split") and (job_dir / "patreon_cut.json").exists():
        # Resume: recover cut index from a prior run
        try:
            cut_beat = int(json.loads(
                (job_dir / "patreon_cut.json").read_text()
            ).get("cut_beat_index"))
            emit("plan", f"Resuming Patreon split after beat {cut_beat + 1}")
        except Exception:  # noqa: BLE001
            cut_beat = None

    # 6. Submit HeyGen clips (host intro + split moments only — the budget)
    host_cfg = profile.get("host", {}) or {}
    av_id = host_cfg.get("heygen_avatar_id") or config.HEYGEN_AVATAR_ID
    av_type = host_cfg.get("heygen_avatar_type") or config.HEYGEN_AVATAR_TYPE
    av_engine = (host_cfg.get("heygen_engine") or "").strip()  # "avatar_v"/"avatar_iv"
    av_res = host_cfg.get("heygen_resolution") or "720p"
    heygen_ok = bool(config.HEYGEN_API_KEY and av_id)
    if not heygen_ok:
        emit("host", "HeyGen not configured (HEYGEN_API_KEY / HEYGEN_AVATAR_ID) "
                     "— host falls back to a still or plain b-roll")
    for i, b in enumerate(beats):
        if not (b["type"] == "host" or b.get("split")):
            continue
        host_file = assets / f"host_{i:03d}.mp4"
        if host_file.exists():  # resume: HeyGen clip already generated
            b["host_file"] = host_file
            emit("host", f"Beat {i+1}: reusing existing HeyGen clip")
            continue
        if not heygen_ok:
            continue
        # A transient network blip here used to silently downgrade ALL host
        # beats to the still-image fallback for the whole video — retry each
        # submit a few times before giving up on that beat.
        for attempt in range(3):
            try:
                if av_engine in ("avatar_v", "avatar_iv"):
                    # v3 platform wants uploaded audio, not a remote URL
                    if not b.get("audio"):
                        break
                    asset_id = heygen.upload_audio_asset(Path(b["audio"]))
                    b["heygen_id"] = heygen.submit_talking_v3(
                        audio_asset_id=asset_id, avatar_id=av_id,
                        resolution=av_res, engine=av_engine)
                    b["heygen_v3"] = True
                elif b.get("audio_url"):
                    b["heygen_id"] = heygen.submit_talking(
                        audio_url=b["audio_url"], width=width, height=height,
                        avatar_id=av_id, avatar_type=av_type)
                elif b.get("audio"):
                    # resumed beat: local audio only — hand it over as an asset
                    asset_id = heygen.upload_audio_asset(Path(b["audio"]))
                    b["heygen_id"] = heygen.submit_talking(
                        audio_asset_id=asset_id, width=width, height=height,
                        avatar_id=av_id, avatar_type=av_type)
                else:
                    break
                emit("host", f"Beat {i+1}: HeyGen clip submitted "
                             f"({b['duration']:.0f}s)")
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    emit("host", f"Beat {i+1}: HeyGen submit failed after "
                                 f"3 tries ({e}) — still-image fallback")
                else:
                    emit("host", f"Beat {i+1}: HeyGen submit retry ({e})")
                    time.sleep(8 * (attempt + 1))

    # 7. Submit AI b-roll (video clips and cheap grok stills)
    mm_places = (mindmap or {}).get("placements", {})

    # Resume: reuse b-roll media already on disk from a prior run, so a
    # retried job only generates/fetches what is actually missing (e.g. the
    # HeyGen clips after a submit outage). The beat index is stable because
    # the plan is persisted.
    reused = 0
    for i, b in enumerate(beats):
        if b["type"] == "host" and not b.get("split"):
            continue  # host visuals live in host_*.mp4, handled in step 10
        found = None
        for ext in ("mp4", "jpg", "png"):
            for name in (f"beat_{i:03d}.{ext}", f"beat_{i:03d}_fb.{ext}"):
                p = assets / name
                if p.exists() and p.stat().st_size > 1000:
                    found = (p, ext, "_fb" in name)
                    break
            if found:
                break
        if not found:
            continue
        p, ext, is_fb = found
        if ext in ("jpg", "png"):
            kind = "image"
        elif is_fb:
            kind = "stock"
        elif i in mm_places or b["type"] == "animation":
            kind = "anim"
        elif b["type"] == "ai_video":
            kind = "video"
        else:
            kind = "stock"
        b["media"], b["media_kind"] = p, kind
        reused += 1
    if reused:
        emit("broll", f"Resuming: reusing {reused}/{len(beats)} b-roll "
                      f"assets already on disk")

    for i, b in enumerate(beats):
        if i in mm_places or b.get("media"):
            continue  # mind-map slide replaces this beat's visual — no spend
        if b["type"] == "ai_video":
            try:
                b["task_id"] = aivideo.submit_clip(
                    b["prompt"], b["duration"],
                    resolution=v.get("ai_video_resolution", "720p"))
                emit("broll", f"Beat {i+1}: AI b-roll submitted")
            except Exception as e:  # noqa: BLE001
                emit("broll", f"Beat {i+1}: submit failed ({e})")
        elif b["type"] == "ai_image":
            try:
                b["img_task_id"] = images.submit_grok_image(b["prompt"])
                emit("broll", f"Beat {i+1}: AI still submitted")
            except Exception as e:  # noqa: BLE001
                emit("broll", f"Beat {i+1}: submit failed ({e})")

    # 8. Local work while providers generate: stock + animations.
    # used_media caps each stock asset at max_broll_repeats appearances.
    # Default 1 — no clip repeats within a video; raise via the profile's
    # video.max_broll_repeats if a long video needs to lean on a small pool
    # instead of falling through to blank cards.
    themes = inputs["anim_themes"]
    anim_count = 0
    used_media: Dict[str, int] = {}
    broll_repeats = int(v.get("max_broll_repeats", 1))
    for i, b in enumerate(beats):
        if b.get("media"):
            continue  # reused from disk (resume) — nothing to fetch
        if i in mm_places:
            # recurring mind-map slide: cursor glides to this beat's point,
            # then the camera zooms into it (always the first checked theme
            # so the slide looks identical on every return)
            emit("anim", f"Beat {i+1}: mind map → node "
                         f"“{mindmap['nodes'][mm_places[i]][:40]}”")
            try:
                b["media"] = animation.render_animation(
                    "mindmap",
                    {"title": mindmap["title"], "nodes": mindmap["nodes"],
                     "focus": mm_places[i]},
                    b["duration"], assets / f"beat_{i:03d}.mp4",
                    width, height, fps, theme=themes[0])
                b["media_kind"] = "anim"
                continue
            except Exception as e:  # noqa: BLE001
                emit("anim", f"Beat {i+1}: mind map failed ({e}) — "
                             f"using planned b-roll")
        if b["type"] == "stock":
            emit("broll", f"Beat {i+1}: stock “{b['query']}”")
            media = footage.fetch_stock(b["query"], assets / f"beat_{i:03d}.mp4",
                                        used=used_media, narration=b["text"],
                                        smart=smart, max_repeats=broll_repeats)
            if media:
                b["media"], b["media_kind"] = media, "stock"
            else:
                _broll_fallback(b, i, assets, width, height, emit,
                                skip_video=True, used=used_media, smart=smart,
                                max_repeats=broll_repeats)
        elif b["type"] == "stock_image":
            emit("broll", f"Beat {i+1}: stock photo “{b['query']}”")
            media = footage.fetch_stock_image(b["query"],
                                              assets / f"beat_{i:03d}.jpg",
                                              used=used_media,
                                              narration=b["text"], smart=smart,
                                              max_repeats=broll_repeats)
            if media:
                b["media"], b["media_kind"] = media, "image"
            else:
                _broll_fallback(b, i, assets, width, height, emit,
                                used=used_media, smart=smart,
                                max_repeats=broll_repeats)
        elif b["type"] == "animation":
            theme = themes[anim_count % len(themes)]
            anim_count += 1
            emit("anim", f"Beat {i+1}: rendering “{b['template']}” "
                         f"animation ({theme})")
            try:
                params = dict(b.get("params") or {})
                if b["template"] == "photocard" and not params.get("image_data"):
                    # the framed photo is a real stock image, embedded inline
                    img = footage.fetch_stock_image(
                        b.get("query", ""), assets / f"beat_{i:03d}_photo.jpg",
                        used=used_media, narration=b["text"], smart=smart,
                        max_repeats=broll_repeats)
                    if img is None:
                        raise RuntimeError("no fitting photo for photocard")
                    import base64
                    params["image_data"] = ("data:image/jpeg;base64," +
                                            base64.b64encode(img.read_bytes()).decode())
                pane_w = width // 2 if b.get("split") else width
                b["media"] = animation.render_animation(
                    b["template"], params, b["duration"],
                    assets / f"beat_{i:03d}.mp4", pane_w, height, fps,
                    theme=theme)
                b["media_kind"] = "anim"
            except Exception as e:  # noqa: BLE001
                emit("anim", f"Beat {i+1}: animation failed ({e})")
                _broll_fallback(b, i, assets, width, height, emit,
                                used=used_media, max_repeats=broll_repeats)

    # 9. Collect AI b-roll (videos + grok stills)
    for i, b in enumerate(beats):
        if b["type"] not in ("ai_video", "ai_image") or i in mm_places:
            continue
        if b.get("media") and not b.get("task_id") and not b.get("img_task_id"):
            continue  # reused from disk (resume) — nothing to collect
        if b["type"] == "ai_image":
            if b.get("img_task_id"):
                try:
                    b["media"] = images.collect_grok_image(
                        b["img_task_id"], assets / f"beat_{i:03d}.png")
                    b["media_kind"] = "image"
                    continue
                except Exception as e:  # noqa: BLE001
                    emit("broll", f"Beat {i+1}: AI still failed ({e})")
            _broll_fallback(b, i, assets, width, height, emit, used=used_media,
                            max_repeats=broll_repeats)
            continue
        if b.get("task_id"):
            emit("broll", f"Beat {i+1}: waiting for AI b-roll…")
            try:
                b["media"] = aivideo.collect_clip(b["task_id"],
                                                  assets / f"beat_{i:03d}.mp4")
                b["media_kind"] = "video"
                continue
            except Exception as e:  # noqa: BLE001
                emit("broll", f"Beat {i+1}: AI b-roll failed ({e})")
        _broll_fallback(b, i, assets, width, height, emit, used=used_media,
                        max_repeats=broll_repeats)

    # 10. Collect HeyGen clips and finalize beat kinds
    still = _host_still(profile, profile_name)
    for i, b in enumerate(beats):
        is_host = b["type"] == "host"
        is_split = bool(b.get("split"))
        if not (is_host or is_split):
            b["kind"] = b.get("media_kind", "blank")
            continue

        host_media, host_kind = None, None
        if b.get("host_file"):
            host_media, host_kind = b["host_file"], "host"
        elif b.get("heygen_id"):
            emit("host", f"Beat {i+1}: waiting for HeyGen clip…")
            try:
                if b.get("heygen_v3"):
                    host_media = heygen.collect_talking_v3(
                        b["heygen_id"], assets / f"host_{i:03d}.mp4",
                        log=lambda m: emit("host", m))
                else:
                    host_media = heygen.collect_talking(
                        b["heygen_id"], assets / f"host_{i:03d}.mp4",
                        audio_path=b.get("audio"), width=width, height=height,
                        avatar_id=av_id, avatar_type=av_type,
                        log=lambda m: emit("host", m))
                host_kind = "host"
            except Exception as e:  # noqa: BLE001
                emit("host", f"Beat {i+1}: HeyGen failed ({e})")
        if host_media is None and still is not None:
            host_media, host_kind = still, "image"

        if is_host:
            if host_media is not None:
                b["kind"], b["media"] = host_kind, host_media
            else:
                emit("host", f"Beat {i+1}: no host visual — falling back to stock")
                b["query"] = " ".join(inputs["title"].split()[:3])
                _broll_fallback(b, i, assets, width, height, emit,
                                used=used_media, max_repeats=broll_repeats)
                b["kind"] = b.get("media_kind", "blank")
        else:  # split
            if host_media is not None:
                b["kind"] = "split"
                b["host_media"], b["host_kind"] = host_media, host_kind
            else:
                b["kind"] = b.get("media_kind", "blank")  # plain b-roll beat

    # 11. Music + render
    music_path = (assets / "music.mp3") if (assets / "music.mp3").exists() else None
    if music_tasks:
        emit("music", "Collecting music tracks…")
        music_path = music.collect_music(music_tasks, assets / "music.mp3", assets)
        emit("music", "Music ready" if music_path else "No music (continuing)")

    overlay = None
    if (v.get("overlay", {}) or {}).get("enabled", True):
        emit("render", "Preparing particle overlay (cached per resolution)…")
        try:
            overlay = animation.ensure_particles(width, height,
                                                 int(v.get("fps", 30)))
        except Exception as e:  # noqa: BLE001
            emit("render", f"overlay unavailable ({e}) — continuing without")

    # Per-author figure: a video citing e.g. "Sigmund Freud" uses
    # figures/freud.png when it exists, else the profile's default figure.
    fig_cfg = (v.get("figure") or {})
    if fig_cfg.get("image") and inputs["cited_philosophers"]:
        slug = inputs["cited_philosophers"].split(",")[0].strip().split()[-1].lower()
        candidate = config.ROOT / "figures" / f"{slug}.png"
        if candidate.exists():
            profile = json.loads(json.dumps(profile))  # don't mutate cache
            profile["video"]["figure"]["image"] = f"figures/{slug}.png"
            emit("render", f"figure: using figures/{slug}.png for "
                           f"{inputs['cited_philosophers']}")

    # corner badge (e.g. Hannah's e-book QR card): profile supplies the image,
    # the per-video toggle turns it on — pinned top-left for the whole video
    badge = None
    if inputs["qr_badge"]:
        badge_rel = (v.get("corner_badge") or "").strip()
        if badge_rel:
            p = Path(badge_rel) if badge_rel.startswith("/") else config.ROOT / badge_rel
            if p.exists():
                badge = p
                emit("render", f"corner badge ON: {badge_rel}")
            else:
                emit("render", f"⚠ corner badge enabled but {badge_rel} is "
                               f"missing — rendering without it")
        else:
            emit("render", "⚠ corner badge enabled but this profile has no "
                           "video.corner_badge image — rendering without it")

    # Spotify popup: timed (10s open + 10s random in first 10 min), not
    # permanent. Language picks DE / NL / EN card art.
    popup = None
    popup_windows = None
    if inputs.get("spotify_popup"):
        lang_l = (inputs.get("language") or "").strip().lower()
        if lang_l.startswith(("german", "deutsch", "de")):
            default_popup = "branding/spotify-popup-de.png"
        elif lang_l.startswith(("dutch", "nederlands", "nl", "flemish", "vlaams")):
            default_popup = "branding/spotify-popup-nl.png"
        else:
            default_popup = "branding/spotify-popup.png"
        popup_rel = (v.get("spotify_popup") or default_popup).strip()
        # If profile forces a path but language has a better localized file
        # and profile path is the generic EN one, prefer localized.
        if popup_rel in ("branding/spotify-popup.png", "spotify-popup.png"):
            popup_rel = default_popup
        p = Path(popup_rel) if popup_rel.startswith("/") else config.ROOT / popup_rel
        if not p.exists() and default_popup != popup_rel:
            p = config.ROOT / default_popup
        if p.exists():
            popup = p
            url = inputs.get("spotify_url") or ""
            # Plan windows from rough total duration (sum of VO lengths)
            est_dur = sum(float(bb.get("duration") or 0) for bb in beats)
            seed_path = job_dir / "spotify_popup.json"
            if seed_path.exists():
                try:
                    saved = json.loads(seed_path.read_text())
                    popup_windows = [tuple(w) for w in saved.get("windows") or []]
                    seed = saved.get("seed")
                except Exception:  # noqa: BLE001
                    popup_windows, seed = [], None
            else:
                seed = abs(hash(job_dir.name)) % (2 ** 31)
                popup_windows = render.plan_spotify_windows(est_dur, seed=seed)
                seed_path.write_text(json.dumps({
                    "seed": seed,
                    "windows": popup_windows,
                    "language": inputs.get("language"),
                    "asset": str(p.relative_to(config.ROOT))
                    if p.is_relative_to(config.ROOT) else str(p),
                    "url": url,
                }, indent=1))
            emit("render",
                 f"Spotify popup ON ({p.name}): "
                 + ", ".join(f"{s:.0f}–{e:.0f}s" for s, e in (popup_windows or []))
                 + (f" · link {url}" if url else ""))
            if url:
                (job_dir / "spotify_url.txt").write_text(
                    f"Spotify: {url}\n"
                    f"(Add this link to the YouTube description / end screen.)\n")
        else:
            emit("render", f"⚠ Spotify popup enabled but {popup_rel} is "
                           f"missing — rendering without it")

    emit("render", "Compositing with FFmpeg…")
    if cut_beat is not None:
        emit("render", f"Patreon mode: free half (1–{cut_beat + 1}) first, "
                       f"then paid half ({cut_beat + 2}–{len(beats)})")
    _check_disk(emit, need_gb=3.0)
    out_path = job_dir / "final.mp4"
    render.render_video(beats, profile, music_path, job_dir, out_path,
                        overlay=overlay, badge=badge, popup=popup,
                        popup_windows=popup_windows,
                        cut_after_beat=cut_beat,
                        log=lambda m: emit("render", m.strip()),
                        gpu_encode=gpu_encode, beat_workers=beat_workers,
                        on_beat_progress=beat_progress)
    total = render.probe_duration(out_path)
    part1 = job_dir / "final_part1.mp4"
    part2 = job_dir / "final_part2.mp4"
    if part1.exists() and part2.exists():
        d1 = render.probe_duration(part1)
        d2 = render.probe_duration(part2)
        emit("done",
             f"Patreon split rendered — free {math.floor(d1 // 60)}m"
             f"{int(d1 % 60):02d}s + paid {math.floor(d2 // 60)}m"
             f"{int(d2 % 60):02d}s")
    else:
        emit("done", f"Video rendered — {math.floor(total // 60)}m"
                     f"{int(total % 60):02d}s")

    # Intermediate beat clips are regenerable for free — reclaim the space.
    # Keep final*.mp4; only drop per-beat composites + precomps.
    for clip in job_dir.glob("beat_*.mp4"):
        clip.unlink(missing_ok=True)
    for seg in job_dir.glob("xseg_*.mp4"):
        seg.unlink(missing_ok=True)

    return out_path
