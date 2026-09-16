"""HyperFrames-only visual engine.

This module intentionally keeps executable source generation deterministic.
The language model may propose a versioned JSON blueprint, but it never writes
HTML or JavaScript.  The compiler below owns the complete executable surface,
escapes all authored copy, constrains motion to an allowlist, and renders inside
the job's private ``hyperframes/`` workspace.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import artifacts, cancel, captions, config, llm, render, telemetry, voiceover


ENGINE_VERSION = 1
CLI_VERSION = "0.8.41"
GSAP_VERSION = "3.14.2"
GSAP_SHA256 = "c174bfce53a729418d57a8ad8625e7247c793a22fef8e2851e3cfa3de9cd8280"
GSAP_VENDOR = config.ROOT / "pipeline" / "assets" / f"gsap-{GSAP_VERSION}.min.js"
SCHEMA_VERSION = 1
REQUIRED_DOCTOR_CHECKS = ("Version", "Node.js", "FFmpeg", "FFprobe", "Chrome")
_DOCTOR_CACHE_SECONDS = 300.0
_DOCTOR_LOCK = threading.Lock()
_DOCTOR_CACHE: Tuple[float, Dict[str, Any]] = (0.0, {})
ALLOWED_LAYOUTS = ("editorial", "split", "statement", "diagram", "steps", "quote")
ALLOWED_TRANSITIONS = ("crossfade", "push-left", "push-up", "iris")
ALLOWED_DIRECTIONS = ("left", "right", "up", "down", "scale", "fade")
ROLE_DIRECTIONS = {
    "kicker": ("left", "right", "up", "fade"),
    "title": ("left", "right", "scale", "fade"),
    "body": ("left", "right", "fade"),
    "accent": ("left", "right", "down", "scale", "fade"),
}
ALLOWED_EASES = (
    "power1.out", "power2.out", "power3.out", "power4.out", "expo.out",
    "circ.out", "sine.out", "back.out(1.4)",
)
FORBIDDEN_SOURCE = (
    "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "<iframe",
    "<object", "<embed", "javascript:", "Date.now", "Math.random",
    "repeat: -1", "repeat:-1", "document.cookie", "localStorage",
    "sessionStorage", "window.location", "eval(", "new Function",
    "http://", "https://", "//cdn", "@import", "url(http",
)


def is_hyperframes_profile(profile: Dict[str, Any]) -> bool:
    return str((profile.get("video") or {}).get("visual_engine") or "").lower() == "hyperframes"


def validate_profile(profile: Dict[str, Any]) -> None:
    """Fail closed when the zero-external-visual contract is weakened."""
    if not is_hyperframes_profile(profile):
        raise ValueError("profile is not configured for HyperFrames")
    video = profile.get("video") or {}
    if video.get("external_visuals_allowed") is not False:
        raise ValueError("HyperFrames profile must set external_visuals_allowed=false")
    mix = video.get("broll_mix") or {}
    forbidden = ("stock", "stock_image", "ai_video", "ai_image", "host")
    if any(float(mix.get(name) or 0) != 0 for name in forbidden):
        raise ValueError("HyperFrames profile cannot enable external visual sources")
    if (profile.get("host") or {}).get("enabled"):
        raise ValueError("HyperFrames profile cannot enable an avatar host")
    if (profile.get("thumbnail") or {}).get("enabled") is not False:
        raise ValueError("HyperFrames profile must set thumbnail.enabled=false")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def _install_vendored_gsap(project: Path) -> Path:
    """Copy the reviewed GSAP build into a job without any network dependency."""
    try:
        digest = hashlib.sha256(GSAP_VENDOR.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"vendored GSAP {GSAP_VERSION} is unavailable: {exc}") from exc
    if digest != GSAP_SHA256:
        raise RuntimeError("vendored GSAP failed its integrity check")
    destination = project / "vendor" / "gsap.min.js"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gsap-", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        shutil.copyfile(GSAP_VENDOR, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _stable_seed(*values: Any) -> int:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str).encode()
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def _sentences(text: str) -> List[str]:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return []
    pieces = re.split(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Þ0-9„»\"'])", cleaned)
    return [p.strip() for p in pieces if p.strip()]


def segment_narration(text: str, target_seconds: float = 12.0,
                      chars_per_second: float = 15.0) -> List[str]:
    """Create semantic narration spans used for TTS before visual planning."""
    target = max(100, int(target_seconds * chars_per_second))
    maximum = max(target + 80, int(target * 1.65))
    output: List[str] = []
    current = ""
    for sentence in _sentences(text):
        if current and len(current) + 1 + len(sentence) > target:
            output.append(current)
            current = ""
        if len(sentence) > maximum:
            words = sentence.split()
            chunk: List[str] = []
            for word in words:
                if chunk and len(" ".join(chunk + [word])) > target:
                    output.append(" ".join(chunk))
                    chunk = []
                chunk.append(word)
            if chunk:
                if current:
                    output.append(current)
                    current = ""
                output.append(" ".join(chunk))
            continue
        current = f"{current} {sentence}".strip()
    if current:
        output.append(current)
    return output or [text.strip()]


def _voice_id(profile: Dict[str, Any], language: str) -> Optional[str]:
    voices = profile.get("voices") or {}
    direct = voices.get(language) or voices.get(language.split()[0] if language else "")
    if direct:
        return str(direct)
    lang = language.casefold()
    for key, value in voices.items():
        if str(key).casefold() == lang or str(key).casefold().startswith(lang):
            return str(value)
    return None


def _escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _merge_narration(beats: Sequence[Dict[str, Any]], project: Path) -> Tuple[Path, Path, float, List[captions.Segment]]:
    assets_dir = project / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    master = assets_dir / "narration.wav"
    merged_srt = assets_dir / "narration.srt"
    concat_file = assets_dir / "narration.concat.txt"
    audio_paths = [Path(b["audio"]) for b in beats]
    contract = {
        "engine": ENGINE_VERSION,
        "audio": [artifacts.signature(p) for p in audio_paths],
        "durations": [round(float(b.get("duration") or 0), 6) for b in beats],
    }
    manifest = assets_dir / "narration.manifest.json"
    if not (artifacts.matches(manifest, contract, [master, merged_srt])
            and render.is_valid_audio(master, min_duration=0.25)):
        _atomic_text(concat_file, "".join(
            f"file '{_escape_concat_path(path)}'\n" for path in audio_paths))
        render._run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-vn", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(master),
        ])
        offset = 0.0
        all_cues: List[captions.Segment] = []
        for beat in beats:
            srt = beat.get("srt")
            if srt and Path(srt).exists():
                for cue in captions.parse_srt(Path(srt).read_text(encoding="utf-8", errors="ignore")):
                    all_cues.append(captions.Segment(
                        max(0.0, offset + cue.start), max(0.0, offset + cue.end), cue.text))
            offset += float(beat.get("duration") or 0)
        captions.write_srt(all_cues, merged_srt)
        artifacts.publish(manifest, contract, [master, merged_srt])
    cues = captions.parse_srt(merged_srt.read_text(encoding="utf-8", errors="ignore"))
    duration = render.probe_duration(master)
    if duration <= 0:
        raise RuntimeError("merged HyperFrames narration is not playable")
    return master, merged_srt, duration, cues


def prepare_narration(narration: str, profile: Dict[str, Any], language: str,
                      project: Path, emit: Callable[[str, str], None]) -> Tuple[List[Dict[str, Any]], Path, Path, float, List[captions.Segment]]:
    """Generate narration first so every later visual time is exact."""
    cfg = (profile.get("video") or {}).get("hyperframes") or {}
    spans = segment_narration(narration, float(cfg.get("target_scene_seconds") or 12))
    beats: List[Dict[str, Any]] = [{"text": text, "type": "hyperframes"} for text in spans]
    assets_dir = project.parent / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    groups = voiceover.group_beats_for_tts(beats, config.AI33_MAX_TTS_CHARS)
    voiceover.generate_voiceover_grouped(
        beats, list(enumerate(groups)), assets_dir,
        voice_id=_voice_id(profile, language),
        emit=lambda message: emit("voiceover", message), total_groups=len(groups))
    master, srt, duration, cues = _merge_narration(beats, project)
    emit("voiceover", f"Narration master ready: {duration:.1f}s across {len(beats)} semantic scenes")
    return beats, master, srt, duration, cues


def _design(profile: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    hf = (profile.get("video") or {}).get("hyperframes") or {}
    palette = hf.get("palette") or {}
    return {
        "schema_version": 1,
        "mood": str(inputs.get("hyperframes_style") or hf.get("style") or "warm editorial"),
        "background": str(palette.get("background") or "#161412"),
        "surface": str(palette.get("surface") or "#27221D"),
        "foreground": str(palette.get("foreground") or "#F4EBDD"),
        "muted": str(palette.get("muted") or "#C8B9A5"),
        "accent": str(palette.get("accent") or "#D4864A"),
        "display_font": str(hf.get("display_font") or "Georgia"),
        "body_font": str(hf.get("body_font") or "Arial"),
        "motion": str(inputs.get("hyperframes_pace") or hf.get("pace") or "measured"),
    }


def write_design(project: Path, design: Dict[str, Any]) -> Path:
    path = project / "DESIGN.md"
    content = f"""# Visual identity

## Style Prompt

{design['mood']}. Editorial hierarchy, edge-anchored layouts, meaningful diagrams,
and restrained depth. The visual language stays coherent while individual scene
layouts and choreography respond to the narration.

## Colors

- Background: `{design['background']}`
- Surface: `{design['surface']}`
- Foreground: `{design['foreground']}`
- Muted text: `{design['muted']}`
- Accent: `{design['accent']}`

## Typography

- Display: `{design['display_font']}`
- Body/data: `{design['body_font']}`

## Motion

{design['motion']} pacing. Entrances use varied direction, duration, and easing;
transitions carry intermediate exits; ambient movement remains subordinate.

## What NOT to Do

- No stock, generated imagery, avatar, or remote visual asset.
- No generic card grid or centered floating web layout.
- No full-screen linear gradient on the dark canvas.
- No unseeded randomness, infinite animation, or jump cut.
- No font, color, or motion choice outside this identity without revision.
"""
    _atomic_text(path, content)
    return path


def _summary(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0]
    return (cut or value[:limit]).rstrip(" ,;:-") + "…"


def _fallback_scene(text: str, index: int, duration: float, seed: int) -> Dict[str, Any]:
    sentences = _sentences(text)
    first = sentences[0] if sentences else text
    if len(sentences) > 1:
        title = _summary(first, 74)
        body = _summary(" ".join(sentences[1:3]), 180)
    else:
        words = first.split()
        if len(words) > 8:
            title = _summary(" ".join(words[:8]), 74)
            body = _summary(" ".join(words[8:]), 180)
        else:
            title = _summary(first, 74)
            body = "A focused moment in the narrative."
    layouts = ALLOWED_LAYOUTS
    transitions = ALLOWED_TRANSITIONS
    local = seed + index * 7919
    entrance = {}
    for n, role in enumerate(("kicker", "title", "body", "accent")):
        directions = ROLE_DIRECTIONS[role]
        entrance[role] = {
            "direction": directions[(local + n * 3) % len(directions)],
            "distance": 28 + ((local >> (n + 1)) % 54),
            "duration": round(0.32 + ((local >> (n + 3)) % 43) / 100, 2),
            "ease": ALLOWED_EASES[(local + n * 5) % len(ALLOWED_EASES)],
            "at": round(0.18 + n * (0.08 + (local % 5) / 100), 2),
        }
    return {
        "id": f"scene-{index + 1:03d}",
        "kicker": f"{index + 1:02d} / MOMENT",
        "title": title,
        "body": body,
        "layout": layouts[(local // 7) % len(layouts)],
        "transition": transitions[(local // 11) % len(transitions)],
        "ambient": ("drift", "breathe", "rotate", "still")[(local // 13) % 4],
        "entrances": entrance,
        "narration": text,
        "duration": round(duration, 6),
    }


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
        if math.isfinite(parsed):
            return min(high, max(low, parsed))
    except (TypeError, ValueError):
        pass
    return default


def _coerce_scene(raw: Any, fallback: Dict[str, Any], index: int) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    scene = dict(fallback)
    for key, limit in (("kicker", 40), ("title", 90), ("body", 240)):
        if str(raw.get(key) or "").strip():
            scene[key] = _summary(str(raw[key]), limit)
    if raw.get("layout") in ALLOWED_LAYOUTS:
        scene["layout"] = raw["layout"]
    if raw.get("transition") in ALLOWED_TRANSITIONS:
        scene["transition"] = raw["transition"]
    if raw.get("ambient") in ("drift", "breathe", "rotate", "still"):
        scene["ambient"] = raw["ambient"]
    proposed = raw.get("entrances") if isinstance(raw.get("entrances"), dict) else {}
    for role, base in scene["entrances"].items():
        candidate = proposed.get(role) if isinstance(proposed.get(role), dict) else {}
        base["direction"] = candidate.get("direction") \
            if candidate.get("direction") in ROLE_DIRECTIONS[role] else base["direction"]
        base["ease"] = candidate.get("ease") \
            if candidate.get("ease") in ALLOWED_EASES else base["ease"]
        base["distance"] = round(_number(candidate.get("distance"), base["distance"], 12, 140), 2)
        max_duration = max(0.18, min(1.6, float(scene["duration"]) * 0.28))
        max_at = max(0.1, min(2.2, float(scene["duration"]) * 0.28))
        base["duration"] = round(_number(
            candidate.get("duration"), min(base["duration"], max_duration),
            min(0.18, max_duration), max_duration), 2)
        base["at"] = round(_number(
            candidate.get("at"), min(base["at"], max_at), 0.1, max_at), 2)
    scene["id"] = f"scene-{index + 1:03d}"
    return scene


def _planner_prompt(rows: Sequence[Dict[str, Any]], design: Dict[str, Any],
                    start_index: int, total: int) -> str:
    payload = [{"scene": start_index + i + 1, "duration": round(float(row["duration"]), 2),
                "narration": row["text"]} for i, row in enumerate(rows)]
    return f"""You are the motion director for a HyperFrames-only narrated video.
Create custom visual direction for scenes {start_index + 1}-{start_index + len(rows)} of {total}.
The result will be compiled by a safe deterministic compiler; do not write HTML,
CSS, JavaScript, markdown, URLs, asset requests, or instructions.

Visual identity: {json.dumps(design, ensure_ascii=False)}

Return ONLY this JSON shape:
{{"scenes":[{{
  "kicker":"max 5 words", "title":"max 12 words", "body":"max 28 words",
  "layout":"editorial|split|statement|diagram|steps|quote",
  "transition":"crossfade|push-left|push-up|iris",
  "ambient":"drift|breathe|rotate|still",
  "entrances":{{
    "kicker":{{"direction":"left|right|up|fade","distance":number,"duration":number,"ease":"power1.out|power2.out|power3.out|power4.out|expo.out|circ.out|sine.out|back.out(1.4)","at":number}},
    "title":{{same fields; direction left|right|scale|fade}},
    "body":{{same fields; direction left|right|fade}},
    "accent":{{same fields; direction left|right|down|scale|fade}}
  }}
}}]}}

Each scene needs a distinct layout and choreography appropriate to its meaning.
Vary entrance direction, duration, ease, and timing within every scene. The
first moving element is the most important. Keep all entrance `at` values in
the first 30% of that scene. Reuse one primary transition coherently; use an
accent transition for topic changes. Do not invent facts not in the narration.

SCENES:
{json.dumps(payload, ensure_ascii=False)}"""


@telemetry.timed("hyperframes_blueprint")
def build_blueprint(beats: Sequence[Dict[str, Any]], design: Dict[str, Any],
                    profile: Dict[str, Any], model: str, project: Path,
                    emit: Callable[[str, str], None]) -> Dict[str, Any]:
    starts: List[float] = []
    elapsed = 0.0
    rows: List[Dict[str, Any]] = []
    for beat in beats:
        starts.append(elapsed)
        duration = float(beat.get("duration") or 0)
        rows.append({"text": beat["text"], "duration": duration})
        elapsed += duration
    seed = _stable_seed([r["text"] for r in rows], design, ENGINE_VERSION)
    fallbacks = [_fallback_scene(row["text"], i, row["duration"], seed)
                 for i, row in enumerate(rows)]
    hf = (profile.get("video") or {}).get("hyperframes") or {}
    authored: List[Any] = []
    if bool((hf.get("authoring") or {}).get("enabled", True)):
        authoring_cfg = hf.get("authoring") or {}
        chunk_size = max(4, min(20, int((hf.get("authoring") or {}).get("scenes_per_call") or 12)))
        max_calls = max(1, int((hf.get("authoring") or {}).get("max_calls") or 8))
        max_tokens = max(1000, min(12000, int((hf.get("authoring") or {}).get("max_tokens_per_call") or 6000)))
        max_credits = max(1, int(authoring_cfg.get("max_kie_credits") or 1000))
        # Conservative reservation rates in Kie credits per million tokens.
        # The profile may update these with a versioned pricing snapshot.
        in_rate = float(authoring_cfg.get("input_credits_per_million") or 500)
        out_rate = float(authoring_cfg.get("output_credits_per_million") or 2000)
        uses_kie = (config.SCRIPT_MODELS.get(model) or {}).get("format") != "cli"
        reserved_credits = 0.0
        budget_rows: List[Dict[str, Any]] = []
        planned_requests: List[Tuple[int, str, int]] = []
        chunks = list(range(0, len(rows), chunk_size))
        if len(chunks) > max_calls:
            raise RuntimeError(
                f"HyperFrames plan needs {len(chunks)} authoring calls, above configured max {max_calls}")
        for start in chunks:
            prompt = _planner_prompt(
                rows[start:start + chunk_size], design, start, len(rows))
            input_tokens = math.ceil(len(prompt) / 4)
            reservation = ((input_tokens * in_rate + max_tokens * out_rate)
                           / 1_000_000) if uses_kie else 0.0
            if reserved_credits + reservation > max_credits:
                raise RuntimeError(
                    "HyperFrames authoring would exceed its Kie ceiling: "
                    f"{reserved_credits + reservation:.1f} > {max_credits} credits")
            reserved_credits += reservation
            planned_requests.append((start, prompt, input_tokens))
            budget_rows.append({
                "scenes": [start + 1, min(len(rows), start + chunk_size)],
                "estimated_input_tokens": input_tokens,
                "max_output_tokens": max_tokens,
                "reserved_kie_credits": round(reservation, 3),
            })
        artifacts.write(project / "authoring-budget.json", {
            "version": 1, "model": model, "uses_kie": uses_kie,
            "ceiling_kie_credits": max_credits,
            "reserved_kie_credits": round(reserved_credits, 3),
            "rates": {"input_per_million": in_rate, "output_per_million": out_rate},
            "requests": budget_rows,
            "note": "Reservations are conservative ceilings; telemetry records provider-reported actual tokens when available.",
        })
        emit(
            "hyperframes",
            (f"Authoring budget reserved before submission: up to {reserved_credits:.1f} "
             f"of {max_credits} Kie credits across {len(planned_requests)} request(s)"
             if uses_kie else
             f"Authoring uses the local CLI across {len(planned_requests)} request(s); Kie cost is 0"))
        telemetry.count("hyperframes_reserved_kie_credits", reserved_credits)
        for start, prompt, _input_tokens in planned_requests:
            cancel.check()
            emit("hyperframes", f"Directing scenes {start + 1}-{min(len(rows), start + chunk_size)}…")
            result = llm.chat_json(
                prompt,
                model, max_tokens=max_tokens, attempts=2)
            values = result.get("scenes")
            authored.extend(values if isinstance(values, list) else [])
    scenes: List[Dict[str, Any]] = []
    for index, fallback in enumerate(fallbacks):
        scene = _coerce_scene(authored[index] if index < len(authored) else {}, fallback, index)
        scene["start"] = round(starts[index], 6)
        scene["duration"] = round(rows[index]["duration"], 6)
        scenes.append(scene)
    blueprint = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "seed": seed,
        "width": int((profile.get("video") or {}).get("width") or 1920),
        "height": int((profile.get("video") or {}).get("height") or 1080),
        "fps": int((profile.get("video") or {}).get("fps") or 30),
        "duration": round(elapsed, 6),
        "design": design,
        "scenes": scenes,
    }
    artifacts.write(project / "video-blueprint.json", blueprint)
    artifacts.write(project / "timeline.json", {
        "schema_version": 1, "duration": blueprint["duration"],
        "scenes": [{"id": s["id"], "start": s["start"], "duration": s["duration"]}
                   for s in scenes],
    })
    return blueprint


def validate_blueprint(blueprint: Dict[str, Any]) -> None:
    scenes = blueprint.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("HyperFrames blueprint has no scenes")
    expected = 0.0
    ids = set()
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"scene {index + 1} is not an object")
        if scene.get("id") in ids:
            raise ValueError(f"duplicate scene id: {scene.get('id')}")
        ids.add(scene.get("id"))
        start = float(scene.get("start") or 0)
        duration = float(scene.get("duration") or 0)
        if duration <= 0:
            raise ValueError(f"scene {index + 1} has invalid duration")
        if abs(start - expected) > 0.05:
            raise ValueError(f"scene {index + 1} does not follow the narration timeline")
        expected += duration
        if scene.get("layout") not in ALLOWED_LAYOUTS:
            raise ValueError(f"scene {index + 1} uses unsupported layout")
        if index < len(scenes) - 1 and scene.get("transition") not in ALLOWED_TRANSITIONS:
            raise ValueError(f"scene {index + 1} has no supported transition")
        entrances = scene.get("entrances") or {}
        if set(entrances) != {"kicker", "title", "body", "accent"}:
            raise ValueError(f"scene {index + 1} is missing entrances")
        eases = {item.get("ease") for item in entrances.values() if isinstance(item, dict)}
        if len(eases) < 3:
            raise ValueError(f"scene {index + 1} must use at least three entrance eases")
    if abs(expected - float(blueprint.get("duration") or 0)) > 0.1:
        raise ValueError("blueprint duration does not match scene durations")


def _entrance_vars(spec: Dict[str, Any]) -> str:
    direction = spec["direction"]
    distance = float(spec["distance"])
    values: Dict[str, Any] = {"opacity": 0}
    if direction == "left":
        values["x"] = -distance
    elif direction == "right":
        values["x"] = distance
    elif direction == "up":
        values["y"] = -distance
    elif direction == "down":
        values["y"] = distance
    elif direction == "scale":
        values["scale"] = 0.82
    values["duration"] = float(spec["duration"])
    values["ease"] = spec["ease"]
    values["immediateRender"] = False
    return json.dumps(values, separators=(",", ":"))


def _scene_markup(scene: Dict[str, Any], index: int) -> str:
    sid = scene["id"]
    title = html.escape(scene["title"])
    body = html.escape(scene["body"])
    kicker = html.escape(scene["kicker"])
    statement = html.escape(_summary(scene["narration"], 46))
    return f'''    <section id="{sid}" class="scene layout-{scene['layout']}" aria-label="Scene {index + 1}" data-layout-allow-overlap data-layout-allow-overflow>
      <div class="scene-depth" data-layout-ignore></div>
      <div class="scene-content">
        <div id="{sid}-kicker" class="kicker">{kicker}</div>
        <h2 id="{sid}-title" class="scene-title">{title}</h2>
        <p id="{sid}-body" class="scene-body">{body}</p>
        <div id="{sid}-accent" class="accent"><span></span><b>{statement}</b></div>
      </div>
    </section>'''


def _transition_js(old: str, new: str, kind: str, at: float, duration: float) -> str:
    t = f"{at:.6f}"
    d = f"{duration:.3f}"
    if kind == "push-left":
        return (f'tl.set("#{new}", {{opacity:1,xPercent:100,zIndex:3}}, {t});\n'
                f'tl.to("#{old}", {{xPercent:-28,duration:{d},ease:"power3.inOut"}}, {t});\n'
                f'tl.to("#{new}", {{xPercent:0,duration:{d},ease:"power3.inOut"}}, {t});\n'
                f'tl.set("#{old}", {{opacity:0,xPercent:0}}, {at + duration:.6f});')
    if kind == "push-up":
        return (f'tl.set("#{new}", {{opacity:1,yPercent:100,zIndex:3}}, {t});\n'
                f'tl.to("#{old}", {{yPercent:-24,duration:{d},ease:"power2.inOut"}}, {t});\n'
                f'tl.to("#{new}", {{yPercent:0,duration:{d},ease:"power2.inOut"}}, {t});\n'
                f'tl.set("#{old}", {{opacity:0,yPercent:0}}, {at + duration:.6f});')
    if kind == "iris":
        return (f'tl.set("#{new}", {{opacity:1,clipPath:"circle(0% at 50% 50%)",zIndex:3}}, {t});\n'
                f'tl.to("#{new}", {{clipPath:"circle(75% at 50% 50%)",duration:{d},ease:"circ.inOut"}}, {t});\n'
                f'tl.set("#{old}", {{opacity:0}}, {at + duration:.6f});')
    return (f'tl.set("#{new}", {{opacity:0,zIndex:3}}, {t});\n'
            f'tl.to("#{new}", {{opacity:1,duration:{d},ease:"sine.inOut"}}, {t});\n'
            f'tl.to("#{old}", {{opacity:0,duration:{d},ease:"sine.inOut"}}, {t});')


def _caption_markup(cues: Sequence[captions.Segment]) -> str:
    return "\n".join(
        f'    <div id="caption-{i}" class="caption" role="presentation">{html.escape(cue.text)}</div>'
        for i, cue in enumerate(cues))


def _timeline_js(blueprint: Dict[str, Any], cues: Sequence[captions.Segment]) -> str:
    scenes = blueprint["scenes"]
    lines = ["window.__timelines = window.__timelines || {};",
             "var tl = gsap.timeline({paused:true});"]
    for index, scene in enumerate(scenes):
        base = float(scene["start"])
        for role in ("kicker", "title", "body", "accent"):
            spec = scene["entrances"][role]
            at = base + float(spec["at"])
            lines.append(
                f'tl.from("#{scene["id"]}-{role}",{_entrance_vars(spec)},{at:.6f});')
        ambient = scene.get("ambient")
        usable = max(0.4, float(scene["duration"]) - 1.2)
        if ambient != "still" and usable > 0.5:
            cycle = min(3.5, max(1.2, usable / 2))
            repeats = max(0, math.ceil(usable / cycle) - 1)
            props = ({"x": 18} if ambient == "drift" else
                     {"scale": 1.06} if ambient == "breathe" else {"rotation": 7})
            props.update({"duration": round(cycle, 3), "ease": "sine.inOut",
                          "repeat": repeats, "yoyo": True})
            lines.append(
                f'tl.to("#{scene["id"]} .scene-depth",{json.dumps(props, separators=(",", ":"))},{base + .35:.6f});')
        if index < len(scenes) - 1:
            transition_duration = min(0.65, max(0.3, float(scene["duration"]) * 0.06))
            transition_at = max(base + 0.5, base + float(scene["duration"]) - transition_duration)
            lines.append(_transition_js(scene["id"], scenes[index + 1]["id"],
                                        scene["transition"], transition_at, transition_duration))
    for index, cue in enumerate(cues):
        start = max(0.0, float(cue.start))
        end = min(float(blueprint["duration"]), max(start + 0.12, float(cue.end)))
        lines.append(f'tl.set("#caption-{index}",{{opacity:1,visibility:"visible"}},{start:.6f});')
        lines.append(f'tl.from("#caption-{index}",{{y:18,opacity:0,duration:0.18,ease:"power3.out",immediateRender:false}},{start:.6f});')
        fade = max(start + 0.05, end - 0.12)
        lines.append(f'tl.to("#caption-{index}",{{opacity:0,scale:0.97,duration:0.12,ease:"power2.in"}},{fade:.6f});')
        lines.append(f'tl.set("#caption-{index}",{{opacity:0,visibility:"hidden"}},{end:.6f});')
    final_scene = scenes[-1]
    exit_at = max(float(final_scene["start"]) + 0.5, float(blueprint["duration"]) - 0.45)
    lines.append(
        f'tl.to("#{final_scene["id"]} .scene-content",{{opacity:0,y:-24,duration:0.4,ease:"power2.in"}},{exit_at:.6f});')
    lines += ['window.__timelines["main"] = tl;', "tl.seek(0);"]
    return "\n      ".join(lines)


@telemetry.timed("hyperframes_compile")
def compile_project(project: Path, blueprint: Dict[str, Any],
                    narration: Path, cues: Sequence[captions.Segment]) -> Path:
    validate_blueprint(blueprint)
    project.mkdir(parents=True, exist_ok=True)
    _install_vendored_gsap(project)
    design = blueprint["design"]
    width, height = int(blueprint["width"]), int(blueprint["height"])
    duration = float(blueprint["duration"])
    scene_markup = "\n".join(_scene_markup(scene, i)
                              for i, scene in enumerate(blueprint["scenes"]))
    caption_markup = _caption_markup(cues)
    source = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width={width}, height={height}" />
  <meta name="hf-content-security-policy" content="pending" />
  <script src="vendor/gsap.min.js"></script>
  <style>
    @font-face {{ font-family:"{html.escape(design['display_font'])}"; src:local("{html.escape(design['display_font'])}"); font-style:normal; }}
    @font-face {{ font-family:"{html.escape(design['body_font'])}"; src:local("{html.escape(design['body_font'])}"); font-style:normal; }}
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; width:{width}px; height:{height}px; overflow:hidden; background:{design['background']}; }}
    body {{ font-family:"{html.escape(design['body_font'])}",sans-serif; color:{design['foreground']}; }}
    #root {{ position:relative; width:100%; height:100%; overflow:hidden; background:{design['background']}; }}
    .scene {{ position:absolute; inset:0; width:100%; height:100%; overflow:hidden; background-color:{design['background']}; opacity:0; }}
    .scene:first-of-type {{ opacity:1; z-index:2; }}
    .scene-depth {{ position:absolute; width:72%; height:72%; right:-14%; top:-20%; border-radius:50%; background:radial-gradient(circle,{design['accent']}45 0%,{design['accent']}12 42%,{design['background']}00 70%); filter:blur(12px); will-change:transform; }}
    .scene::after {{ content:""; position:absolute; left:7%; right:7%; bottom:7%; height:2px; background:{design['accent']}; opacity:.48; transform-origin:left; }}
    .scene-content {{ position:relative; z-index:2; width:100%; height:100%; padding:clamp(50px,8vh,88px) clamp(70px,6.55vw,126px) clamp(190px,26vh,280px); display:flex; flex-direction:column; justify-content:flex-start; gap:clamp(14px,2.2vh,24px); }}
    .kicker {{ color:{design['accent']}; font-size:clamp(16px,1.15vw,22px); font-weight:700; letter-spacing:.18em; text-transform:uppercase; will-change:transform,opacity; }}
    .scene-title {{ margin:0; max-width:1500px; font-family:"{html.escape(design['display_font'])}",serif; font-size:clamp(52px,5vw,96px); line-height:.98; letter-spacing:-.035em; font-weight:700; text-wrap:balance; will-change:transform,opacity; }}
    .scene-body {{ display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3; overflow:hidden; margin:0; max-width:1120px; color:{design['muted']}; font-size:clamp(22px,1.67vw,32px); line-height:1.34; font-weight:350; will-change:transform,opacity; }}
    .accent {{ display:flex; align-items:center; gap:18px; max-width:980px; margin-top:clamp(4px,1.6vh,18px); color:{design['foreground']}; font-size:clamp(16px,1.05vw,20px); line-height:1.3; will-change:transform,opacity; }}
    .accent span {{ display:block; flex:0 0 82px; height:5px; background:{design['accent']}; }}
    .accent b {{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }}
    .layout-split .scene-content {{ padding-left:52%; }}
    .layout-split .scene-title {{ font-size:clamp(48px,4vw,78px); }}
    .layout-split::before {{ content:""; position:absolute; left:8%; top:16%; width:36%; height:68%; border:2px solid {design['accent']}; border-radius:32px; opacity:.48; }}
    .layout-statement .scene-title {{ font-size:clamp(64px,6.35vw,122px); max-width:1660px; }}
    .layout-statement .scene-body {{ max-width:850px; }}
    .layout-diagram .scene-content {{ padding-right:42%; }}
    .layout-diagram::before {{ content:""; position:absolute; right:9%; top:20%; width:27%; aspect-ratio:1; border:3px solid {design['accent']}; border-radius:50%; box-shadow:0 0 0 34px {design['accent']}16,0 0 0 72px {design['accent']}09; }}
    .layout-steps .scene-title {{ max-width:1200px; }}
    .layout-steps .accent {{ border:1px solid {design['accent']}66; border-radius:999px; padding:16px 28px; }}
    .layout-quote .scene-title::before {{ content:"“"; color:{design['accent']}; margin-right:12px; }}
    .caption {{ position:absolute; z-index:20; left:0; bottom:6.3vh; width:100%; padding:0 9.4vw; text-align:center; color:{design['foreground']}; font-size:clamp(28px,2.2vw,42px); line-height:1.2; font-weight:700; text-shadow:0 3px 16px {design['background']}; opacity:0; visibility:hidden; will-change:transform,opacity; }}
  </style>
</head>
<body>
  <main id="root" data-composition-id="main" data-start="0" data-duration="{duration:.6f}" data-width="{width}" data-height="{height}" data-fps="{int(blueprint['fps'])}">
{scene_markup}
{caption_markup}
    <audio id="narration" src="assets/{html.escape(narration.name)}" data-start="0" data-duration="{duration:.6f}" data-track-index="20" data-volume="1"></audio>
  </main>
  <script>
      {_timeline_js(blueprint, cues)}
  </script>
</body>
</html>
'''
    style_match = re.search(r"<style>(.*?)</style>", source, flags=re.DOTALL)
    script_matches = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source,
                                flags=re.DOTALL)
    if style_match is None or not script_matches:
        raise RuntimeError("compiled HyperFrames source is missing executable blocks")
    policy = (
        "default-src 'none'; "
        # HyperFrames injects its seek/inspection adapters as inline scripts
        # during check and render. Executable source remains compiler-owned,
        # while this policy still blocks every remote script and connection.
        "script-src 'self' 'unsafe-inline'; "
        # The checker also injects an inline measurement stylesheet.
        "style-src 'unsafe-inline'; "
        "font-src 'self'; media-src 'self'; img-src 'self' data:; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    source = source.replace(
        '<meta name="hf-content-security-policy" content="pending" />',
        f'<meta http-equiv="Content-Security-Policy" content="{policy}" />', 1)
    for forbidden in FORBIDDEN_SOURCE:
        if forbidden.casefold() in source.casefold():
            raise ValueError(f"compiled HyperFrames source contains forbidden token: {forbidden}")
    index = project / "index.html"
    _atomic_text(index, source)
    _atomic_text(project / "hyperframes.json", json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "media": {"autoProxy": True},
    }, indent=2))
    _atomic_text(project / "package.json", json.dumps({
        "name": "salcube-hyperframes-job", "private": True, "type": "module",
        "scripts": {
            "check": f"npx --yes hyperframes@{CLI_VERSION} check --json",
            "render": f"npx --yes hyperframes@{CLI_VERSION} render",
        },
    }, indent=2))
    _atomic_text(project / "meta.json", json.dumps({
        "engine_version": ENGINE_VERSION, "schema_version": SCHEMA_VERSION,
        "duration": duration, "scenes": len(blueprint["scenes"]),
    }, indent=2))
    return index


class HyperFramesCommandError(RuntimeError):
    def __init__(self, message: str, *, returncode: int, stdout: str, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _read_spooled_log(handle, limit: int = 16 * 1024 * 1024) -> str:
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    if size > limit:
        handle.seek(-limit, os.SEEK_END)
        prefix = f"[output truncated; retained last {limit} bytes]\n"
    else:
        handle.seek(0)
        prefix = ""
    return prefix + handle.read().decode("utf-8", errors="replace")


def _run_process(command: Sequence[str], cwd: Path, timeout: float) -> Tuple[str, str]:
    """Run chatty CLI commands without filling OS pipe buffers."""
    cancel.check()
    with tempfile.TemporaryFile(mode="w+b") as out, \
            tempfile.TemporaryFile(mode="w+b") as err:
        proc = subprocess.Popen(list(command), cwd=str(cwd), stdout=out, stderr=err)
        token = cancel.current()
        if token:
            token.register(proc)
        started = time.monotonic()
        try:
            while proc.poll() is None:
                cancel.check()
                if time.monotonic() - started > timeout:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise RuntimeError(
                        f"command timed out after {timeout:.0f}s: {' '.join(command[:4])}")
                time.sleep(0.2)
            stdout, stderr = _read_spooled_log(out), _read_spooled_log(err)
        finally:
            if token:
                token.unregister(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    if proc.returncode != 0:
        stdout_tail = "\n".join(stdout.splitlines()[-30:])
        stderr_tail = "\n".join(stderr.splitlines()[-30:])
        tail = "\n".join(part for part in (stdout_tail, stderr_tail) if part)
        raise HyperFramesCommandError(
            f"HyperFrames command failed ({proc.returncode}): {tail}",
            returncode=int(proc.returncode), stdout=stdout, stderr=stderr)
    return stdout or "", stderr or ""


def _json_object(raw: str, label: str) -> Dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"{label} did not return a JSON object")
    try:
        value = json.loads(raw[start:end + 1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} did not return a JSON object")
    return value


def doctor(*, force: bool = False) -> Dict[str, Any]:
    """Return a cached, fail-closed readiness result for local rendering."""
    global _DOCTOR_CACHE
    with _DOCTOR_LOCK:
        checked_at, cached = _DOCTOR_CACHE
        if not force and cached and time.monotonic() - checked_at < _DOCTOR_CACHE_SECONDS:
            return dict(cached)
    health: Dict[str, Any] = {
        "node": shutil.which("node"), "npx": shutil.which("npx"),
        "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
        "node_major": _node_major(), "cli_version": CLI_VERSION,
        "engine_version": ENGINE_VERSION, "required_checks": {}, "ready": False,
    }
    missing = [name for name in ("node", "npx", "ffmpeg", "ffprobe")
               if not health[name]]
    if not missing and health["node_major"] >= 22:
        try:
            stdout, _stderr = _run_process([
                "npx", "--yes", f"hyperframes@{CLI_VERSION}", "doctor", "--json",
            ], config.ROOT, timeout=180)
            report = _json_object(stdout, "HyperFrames doctor")
            checks = {str(row.get("name")): bool(row.get("ok"))
                      for row in report.get("checks") or [] if isinstance(row, dict)}
            health["required_checks"] = {
                name: checks.get(name, False) for name in REQUIRED_DOCTOR_CHECKS}
            health["ready"] = all(health["required_checks"].values())
            health["doctor"] = report
        except Exception as exc:  # noqa: BLE001
            health["error"] = str(exc)[-2000:]
    else:
        health["missing"] = missing
        health["error"] = (f"missing {', '.join(missing)}" if missing else
                           f"Node 22+ required (found {health['node_major'] or 'unknown'})")
    with _DOCTOR_LOCK:
        _DOCTOR_CACHE = (time.monotonic(), dict(health))
    return health


def ensure_runtime() -> Dict[str, Any]:
    """Fail before paid work when the local HyperFrames toolchain is absent."""
    health = doctor()
    missing = [name for name in ("node", "npx", "ffmpeg", "ffprobe")
               if not health[name]]
    if missing or health["node_major"] < 22 or health.get("ready") is not True:
        failed = [name for name, ok in (health.get("required_checks") or {}).items()
                  if not ok]
        raise RuntimeError(
            "HyperFrames runtime unavailable: "
            + (f"missing {', '.join(missing)}; " if missing else "")
            + (f"Node 22+ required (found {health['node_major'] or 'unknown'}); "
               if health["node_major"] < 22 else "")
            + (f"failed checks: {', '.join(failed)}" if failed else
               str(health.get("error") or "doctor did not confirm readiness")))
    return health


def runtime_capability() -> Dict[str, Any]:
    """Small, path-free capability record suitable for fleet heartbeats."""
    health = doctor()
    return {
        "ready": health.get("ready") is True,
        "engine_version": ENGINE_VERSION,
        "cli_version": CLI_VERSION,
        "node_major": int(health.get("node_major") or 0),
        "required_checks": dict(health.get("required_checks") or {}),
        "error": str(health.get("error") or "")[-500:],
    }


def _node_major() -> int:
    try:
        value = subprocess.run(["node", "--version"], capture_output=True,
                               text=True, timeout=5, check=True).stdout.strip()
        return int(value.lstrip("v").split(".", 1)[0])
    except Exception:  # noqa: BLE001
        return 0


@telemetry.timed("hyperframes_check")
def check_project(project: Path, samples: int = 15) -> Dict[str, Any]:
    ensure_runtime()
    command_error: Optional[HyperFramesCommandError] = None
    try:
        stdout, stderr = _run_process([
            "npx", "--yes", f"hyperframes@{CLI_VERSION}", "check", ".", "--json",
            "--samples", str(max(5, samples)), "--at-transitions", "--snapshots",
            "--frame-check",
            "--caption-zone", "x0=0;y0=.80;x1=1;y1=1;severity=warning;seek=.5,1",
        ], project, timeout=900)
    except HyperFramesCommandError as exc:
        command_error = exc
        stdout, stderr = exc.stdout, exc.stderr
        try:
            result = _json_object(stdout, "HyperFrames check")
        except RuntimeError:
            raise RuntimeError(f"HyperFrames validation command failed: {exc}") from exc
    else:
        result = _json_object(stdout, "HyperFrames check")
    if not isinstance(result.get("ok"), bool):
        raise RuntimeError("HyperFrames validation report is missing boolean ok")
    artifacts.write(project / "validation-report.json", result)
    if command_error is not None or result["ok"] is not True:
        raise RuntimeError(
            "HyperFrames validation failed: "
            + json.dumps(result, ensure_ascii=False)[-4000:])
    return result


@telemetry.timed("hyperframes_render")
def render_project(project: Path, output: Path, fps: int, quality: str = "delivery",
                   gpu: bool = False, workers: int = 1) -> Path:
    draft = project / "rendered.mp4"
    command = [
        "npx", "--yes", f"hyperframes@{CLI_VERSION}", "render", ".",
        "--output", str(draft), "--fps", str(fps), "--quality", quality,
        "--workers", str(max(1, min(8, workers))), "--strict",
    ]
    if gpu:
        command.append("--gpu")
    _run_process(command, project, timeout=43_200)
    if not render.is_valid_video(draft):
        raise RuntimeError("HyperFrames render did not produce a playable video")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    shutil.move(str(draft), str(output))
    return output


def publish_delivery_receipts(job_dir: Path, project: Path, blueprint_path: Path,
                              output: Path, fps: int,
                              source_signature: Optional[list] = None,
                              *, gpu: bool = False) -> None:
    """Publish the same verified delivery contract as the legacy renderer."""
    export_contract = {
        "version": 2,
        "source": source_signature,
        "fps": int(fps),
        "quality": "balanced",
        "gpu": bool(gpu),
        "engine": "hyperframes",
        "engine_version": ENGINE_VERSION,
    }
    artifacts.publish(output.with_suffix(".export.json"), export_contract,
                      [output], hash_outputs=True)
    render_contract = {
        "version": 1,
        "engine": "hyperframes",
        "engine_version": ENGINE_VERSION,
        "runtime": CLI_VERSION,
        "blueprint": artifacts.file_digest(blueprint_path),
        "fps": int(fps),
    }
    artifacts.publish(job_dir / "render.manifest.json", render_contract,
                      [output], hash_outputs=True)
    artifacts.publish(project / "final.manifest.json", render_contract,
                      [output], hash_outputs=True)


def run(narration: str, inputs: Dict[str, Any], profile: Dict[str, Any],
        job_dir: Path, model: str, emit: Callable[[str, str], None],
        *, gpu_encode: bool = False, workers: int = 1) -> Path:
    """Complete narration-first HyperFrames path used by the orchestrator."""
    validate_profile(profile)
    project = job_dir / "hyperframes"
    project.mkdir(parents=True, exist_ok=True)
    design = _design(profile, inputs)
    write_design(project, design)  # hard gate: always before source generation
    beats, master, _srt, duration, cues = prepare_narration(
        narration, profile, inputs.get("language") or "", project, emit)
    blueprint_path = project / "video-blueprint.json"
    contract = {
        "engine": ENGINE_VERSION, "narration": artifacts.file_digest(job_dir / "script.txt"),
        "durations": [round(float(b.get("duration") or 0), 6) for b in beats],
        "design": design, "model": model,
    }
    manifest = project / "blueprint.manifest.json"
    if artifacts.matches(manifest, contract, [blueprint_path]):
        blueprint = artifacts.read(blueprint_path)
        emit("hyperframes", f"Resuming: reusing {len(blueprint.get('scenes') or [])}-scene blueprint")
    else:
        blueprint = build_blueprint(beats, design, profile, model, project, emit)
        # The decoded master is authoritative; absorb tiny MP3 concat/probe drift
        # in the final scene so root/audio durations remain exactly equal.
        delta = duration - float(blueprint["duration"])
        blueprint["scenes"][-1]["duration"] = round(
            float(blueprint["scenes"][-1]["duration"]) + delta, 6)
        blueprint["duration"] = round(duration, 6)
        artifacts.write(blueprint_path, blueprint)
        artifacts.publish(manifest, contract, [blueprint_path])
    validate_blueprint(blueprint)
    compile_project(project, blueprint, master, cues)
    emit("hyperframes", f"Validating {len(blueprint['scenes'])} scenes and {len(cues)} captions…")
    check_project(project, samples=15 if duration < 900 else 21)
    emit("render", f"Rendering HyperFrames visual master ({duration / 60:.1f} min)…")
    output = job_dir / "final.mp4"
    render_project(project, output, int(blueprint["fps"]),
                   quality=str(((profile.get("video") or {}).get("hyperframes") or {}).get("quality") or "delivery"),
                   gpu=gpu_encode, workers=workers)
    source_signature = artifacts.signature(output)
    render.encode_final_export(
        output, job_dir, fps=int(blueprint["fps"]), quality="balanced",
        gpu=gpu_encode, log=lambda message: emit("render", message.strip()))
    publish_delivery_receipts(job_dir, project, blueprint_path, output,
                              int(blueprint["fps"]), source_signature,
                              gpu=gpu_encode)
    emit("done", f"HyperFrames video rendered — {duration / 60:.1f} minutes")
    return output
