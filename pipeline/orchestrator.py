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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (animation, aivideo, config, footage, heygen, images, llm,
               music, render, script, voiceover)

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
    """Abort early with a clear message instead of dying mid-render."""
    free_gb = shutil.disk_usage("/").free / 1e9
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
                    used: Optional[set] = None, smart: str = "off") -> None:
    """Fallback chain: stock video → stock image → AI image → blank."""
    query = b.get("query") or " ".join(b["text"].split()[:3])
    if not skip_video:
        media = footage.fetch_stock(query, assets / f"beat_{i:03d}_fb.mp4",
                                    used=used, narration=b["text"], smart=smart)
        if media:
            b["media"], b["media_kind"] = media, "stock"
            return
    media = footage.fetch_stock_image(query, assets / f"beat_{i:03d}_fb.jpg",
                                      used=used, narration=b["text"],
                                      smart=smart)
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
                 voice_gate: Optional[Callable[[Dict[str, Any]], None]] = None) -> Path:
    def emit(step: str, msg: str) -> None:
        print(f"[{step}] {msg}")
        if progress:
            progress(step, msg)

    job_dir.mkdir(parents=True, exist_ok=True)
    assets = job_dir / "assets"
    assets.mkdir(exist_ok=True)

    render.ensure_ffmpeg()
    _check_disk(emit)
    profile = load_profile(profile_name)
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
        "ebook_text": (inputs.get("ebook_text") or "").strip(),
        "broll": broll,
        "mindmap": bool(inputs.get("mindmap", False)),
        "anim_templates": inputs.get("anim_templates"),
        "anim_themes": [t for t in (inputs.get("anim_themes")
                                    or v.get("anim_themes_default")
                                    or ["classic"])
                        if t in animation.THEMES] or ["classic"],
        "smart_stock": (inputs.get("smart_stock")
                        or v.get("smart_stock") or "vision").strip().lower(),
        "qr_badge": bool(inputs.get("qr_badge", False)),
    }
    if inputs["smart_stock"] not in ("vision", "text", "off"):
        inputs["smart_stock"] = "vision"
    smart = inputs["smart_stock"]
    model = llm.set_model(inputs.get("script_model"))
    emit("start", f"“{inputs['title']}” · {inputs['language']} · "
                  f"{inputs['length_chars']} chars · script model: {model} · "
                  f"b-roll: {', '.join(broll)} · stock matching: {smart}"
                  + (" · ebook CTA" if inputs["promote_ebook"] else ""))

    # 1+2. Outline, gate, and full script — skipped when the job dir already
    # has a script.txt (crash resume: the same dir is re-run and every asset
    # already on disk is reused instead of re-billed).
    script_file = job_dir / "script.txt"
    if script_file.exists():
        narration = script_file.read_text()
        emit("script", f"Resuming: reusing existing script ({len(narration)} chars)")
    else:
        emit("outline", f"Writing detailed outline + hooks ({model})…")
        stage1 = script.outline_and_hooks(inputs, profile,
                                          log=lambda m: emit("outline", m))
        (job_dir / "outline.txt").write_text(stage1["outline"])
        (job_dir / "hooks.json").write_text(json.dumps(stage1["hooks"], indent=2))

        if inputs["auto_hook"]:
            chosen = (inputs["auto_hook"] if inputs["auto_hook"] in stage1["hooks"]
                      else next(iter(stage1["hooks"])))
            hook_text, outline = stage1["hooks"][chosen], stage1["outline"]
            emit("outline", f"auto-approved with the {chosen} hook")
        elif gate:
            emit("awaiting", "Waiting for your hook choice + outline approval…")
            hook_text, outline = gate(stage1["hooks"], stage1["outline"])
            emit("outline", "Approved — writing the full script")
        else:
            hook_text, outline = next(iter(stage1["hooks"].values())), stage1["outline"]

        emit("script", "Writing the full script…")
        narration = script.write_script(inputs, profile, outline, hook_text,
                                        log=lambda m: emit("script", m))
        if inputs["promote_ebook"] and inputs.get("ebook_cta_at"):
            emit("script", f"Weaving e-book CTA near "
                           f"{inputs['ebook_cta_at']} chars…")
            try:
                narration = script.insert_ebook_cta(
                    narration, inputs, profile, inputs["ebook_cta_at"],
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
                    narration, pos, inputs["language"],
                    log=lambda m: emit("script", m))
            except Exception as e:  # noqa: BLE001
                emit("script", f"Patreon cut insertion failed ({e}) — "
                               f"continuing WITHOUT a cut")
        script_file.write_text(narration)
        emit("script", f"Narration ready ({len(narration)} chars)")

    # Patreon split: the writer places a marker line that must never be
    # spoken — strip it, remember where it fell, report the cut point later.
    cut_offset = None
    m = re.search(r"\[——[^\]\n]*——\]", narration)
    if m:
        cut_offset = m.start()
        narration = narration[:m.start()] + narration[m.end():]

    # 3. Plan b-roll beats, then place the host inside the credit budget.
    # The plan is persisted so a resumed run works with identical beats.
    plan_file = job_dir / "plan.json"
    mindmap = None
    if plan_file.exists():
        saved = json.loads(plan_file.read_text())
        beats, music_prompt = saved["beats"], saved["music_prompt"]
        mindmap = saved.get("mindmap")
        emit("plan", f"Resuming: reusing existing beat plan ({len(beats)} beats)")
    else:
        emit("plan", "Planning b-roll beats…")
        beats, music_prompt = script.plan_beats(
            narration, profile, broll, anim_templates=inputs["anim_templates"],
            language=inputs["language"], log=lambda m: emit("plan", m))
        beats = script.mark_host_segments(beats, profile,
                                          log=lambda m: emit("plan", m))
        if inputs["mindmap"]:
            emit("plan", "Building the mind-map slide…")
            mindmap = script.plan_mindmap(inputs, "", beats,
                                          log=lambda m: emit("plan", m))
        plan_file.write_text(json.dumps(
            {"beats": beats, "music_prompt": music_prompt,
             "mindmap": mindmap}, indent=1))
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
    voices = profile.get("voices", {})
    lang = inputs["language"]
    voice_id = voices.get(lang) or voices.get(lang.split()[0]) or None

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

    def _tts_one(i: int, b: Dict[str, Any]) -> None:
        dest = assets / f"vo_{i:03d}.mp3"
        if dest.exists():  # resume: TTS already paid for
            srt = dest.with_suffix(".srt")
            _fix_srt(srt)
            b.update(audio=dest, srt=srt if srt.exists() else None,
                     audio_url=None, duration=render.probe_duration(dest))
            return
        for attempt in range(3):  # a flaky beat must not kill the whole run
            try:
                audio, srt, audio_url = voiceover.generate_voiceover(
                    b["text"], dest, voice_id=voice_id)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise
                emit("voiceover", f"Beat {i+1}: TTS retry ({e})")
                import time as _t
                _t.sleep(8 * (attempt + 1))
        _fix_srt(srt)
        b.update(audio=audio, srt=srt, audio_url=audio_url,
                 duration=render.probe_duration(audio) if audio else 4.0)
        emit("voiceover", f"Beat {i+1}/{len(beats)}: narrated")

    # 4 parallel workers: a 3-hour script has 250+ beats — serial TTS would
    # take hours. ai33 jobs poll server-side, so concurrency is safe.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda ib: _tts_one(*ib), enumerate(beats)))

    # Patreon cut report (needs the TTS durations above)
    if cut_offset is not None:
        acc, cut_beat = 0, len(beats) - 1
        for i, b in enumerate(beats):
            acc += len(b["text"]) + 1
            if acc >= cut_offset:
                cut_beat = i
                break
        tr_cfg = v.get("transitions", {}) or {}
        tr = float(tr_cfg.get("seconds", 0.5)) if tr_cfg.get("enabled", True) else 0.0
        t = sum(float(bb.get("duration", 0)) for bb in beats[:cut_beat + 1]) + cut_beat * tr
        stamp = f"{int(t // 60)}m{int(t % 60):02d}s"
        (job_dir / "patreon_cut.txt").write_text(
            f"Patreon cut: after beat {cut_beat + 1} of {len(beats)}, ≈ {stamp} "
            f"into the final video\n")
        emit("plan", f"Patreon cut after beat {cut_beat + 1} (≈ {stamp}) — "
                     f"saved to patreon_cut.txt")
        if voice_gate:
            total = (sum(float(bb.get("duration", 0)) for bb in beats)
                     + max(0, len(beats) - 1) * tr)
            total_stamp = f"{int(total // 60)}m{int(total % 60):02d}s"
            emit("awaiting", f"Voice ready: total ≈ {total_stamp}, Patreon "
                             f"cut ≈ {stamp}. Waiting for your approval to "
                             f"start generating visuals…")
            voice_gate({"cut_time": stamp, "cut_beat": cut_beat + 1,
                        "total_time": total_stamp, "beats": len(beats)})
            emit("plan", "Voice approved — generating visuals")

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
        try:
            if av_engine in ("avatar_v", "avatar_iv"):
                # v3 platform wants uploaded audio, not a remote URL
                if not b.get("audio"):
                    continue
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
                continue
            emit("host", f"Beat {i+1}: HeyGen clip submitted "
                         f"({b['duration']:.0f}s)")
        except Exception as e:  # noqa: BLE001
            emit("host", f"Beat {i+1}: HeyGen submit failed ({e})")

    # 7. Submit AI b-roll (video clips and cheap grok stills)
    mm_places = (mindmap or {}).get("placements", {})
    for i, b in enumerate(beats):
        if i in mm_places:
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
    # used_media guarantees no stock asset ever appears twice in one video.
    themes = inputs["anim_themes"]
    anim_count = 0
    used_media: set = set()
    for i, b in enumerate(beats):
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
                                        smart=smart)
            if media:
                b["media"], b["media_kind"] = media, "stock"
            else:
                _broll_fallback(b, i, assets, width, height, emit,
                                skip_video=True, used=used_media, smart=smart)
        elif b["type"] == "stock_image":
            emit("broll", f"Beat {i+1}: stock photo “{b['query']}”")
            media = footage.fetch_stock_image(b["query"],
                                              assets / f"beat_{i:03d}.jpg",
                                              used=used_media,
                                              narration=b["text"], smart=smart)
            if media:
                b["media"], b["media_kind"] = media, "image"
            else:
                _broll_fallback(b, i, assets, width, height, emit,
                                used=used_media, smart=smart)
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
                        used=used_media, narration=b["text"], smart=smart)
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
                                used=used_media)

    # 9. Collect AI b-roll (videos + grok stills)
    for i, b in enumerate(beats):
        if b["type"] not in ("ai_video", "ai_image") or i in mm_places:
            continue
        if b["type"] == "ai_image":
            if b.get("img_task_id"):
                try:
                    b["media"] = images.collect_grok_image(
                        b["img_task_id"], assets / f"beat_{i:03d}.png")
                    b["media_kind"] = "image"
                    continue
                except Exception as e:  # noqa: BLE001
                    emit("broll", f"Beat {i+1}: AI still failed ({e})")
            _broll_fallback(b, i, assets, width, height, emit, used=used_media)
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
        _broll_fallback(b, i, assets, width, height, emit, used=used_media)

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
                                used=used_media)
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

    emit("render", "Compositing with FFmpeg…")
    _check_disk(emit, need_gb=3.0)
    out_path = job_dir / "final.mp4"
    render.render_video(beats, profile, music_path, job_dir, out_path,
                        overlay=overlay, badge=badge,
                        log=lambda m: emit("render", m.strip()))
    total = render.probe_duration(out_path)

    # Intermediate beat clips are regenerable for free — reclaim the space.
    for clip in job_dir.glob("beat_*.mp4"):
        clip.unlink(missing_ok=True)
    for seg in job_dir.glob("xseg_*.mp4"):
        seg.unlink(missing_ok=True)

    emit("done", f"Video rendered — {math.floor(total // 60)}m{int(total % 60)}s")
    return out_path
