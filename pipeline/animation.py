"""VidRush-style animation templates, rendered locally (no API cost).

Templates are standalone HTML files in anim_templates/ that expose two JS
functions: setup(params, duration) and seek(t). The renderer drives them
deterministically with headless Chromium — one screenshot per frame — and
assembles the frames with ffmpeg. Any duration works because time is
seeked, not played.

Template params are chosen by the beat planner (see script.plan_beats):
  checklist: {"title": str, "items": [str, ...]}
  stat:      {"number": num, "suffix": str, "label": str, "note": str}
  compare:   {"title": str, "left": {"title": str, "items": [...]},
              "right": {"title": str, "items": [...]}}
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict

from . import config

TEMPLATES = ("checklist", "stat", "compare", "quote", "keyword", "barchart",
             "list", "warning", "timeline", "gauge", "didyouknow", "mythfact",
             "definition", "trio", "photocard", "chapter", "titlecard")

# Color schemes: every template is authored in "classic" (cream + teal); other
# themes swap the teal accent family by hex substitution at render time.
# Keys per theme: primary, gradient partner, light (on dark bg), tint, and the
# raw r,g,b triple used inside rgba() glows.
THEMES: Dict[str, Dict[str, str]] = {
    "classic": {},
    "crimson": {"#2C8C8C": "#C44536", "#3aa8a8": "#d4604f",
                "#7fd4d4": "#f0a294", "#e7f3f1": "#f9e6e2",
                "44,140,140": "196,69,54"},
    "amber": {"#2C8C8C": "#D99114", "#3aa8a8": "#e6a83a",
              "#7fd4d4": "#f3cf8e", "#e7f3f1": "#f9efdc",
              "44,140,140": "217,145,20"},
    "forest": {"#2C8C8C": "#55884D", "#3aa8a8": "#6ba062",
               "#7fd4d4": "#b9d8b3", "#e7f3f1": "#e9f2e6",
               "44,140,140": "85,136,77"},
    "plum": {"#2C8C8C": "#8E5A9E", "#3aa8a8": "#a273b0",
             "#7fd4d4": "#d3b8dc", "#e7f3f1": "#f1e8f4",
             "44,140,140": "142,90,158"},
}


def _themed_template(template: str, theme: str, workdir: Path) -> Path:
    """Return the template file, re-colored for `theme` if needed."""
    src = config.ANIM_DIR / f"{template}.html"
    mapping = THEMES.get(theme) or {}
    if not mapping:
        return src
    html = src.read_text()
    for old, new in mapping.items():
        html = html.replace(old, new).replace(old.upper(), new).replace(old.lower(), new)
    themed = (workdir / f"_{template}_{theme}.html").resolve()
    themed.write_text(html)
    return themed

OVERLAYS_DIR = config.ROOT / "overlays"

# ensure_particles() writes to a resolution-keyed shared path — with several
# videos rendering at once, two jobs hitting the same resolution for the
# first time would both try to render into (and delete) the same frames_dir
# concurrently. One lock per resolution serializes just that first render;
# every later call for the same resolution hits the on-disk cache instead.
_PARTICLE_LOCKS: Dict[Path, threading.Lock] = {}
_PARTICLE_LOCKS_GUARD = threading.Lock()


def ensure_particles(width: int, height: int, fps: int,
                     seconds: float = 16.0) -> Path:
    """Dust/twinkle overlay (white on black, screen-blended at render time).

    Rendered once per resolution and cached — it costs nothing afterwards.
    """
    OVERLAYS_DIR.mkdir(exist_ok=True)
    dest = OVERLAYS_DIR / f"particles_{width}x{height}_{fps}.mp4"
    if dest.exists():
        return dest
    with _PARTICLE_LOCKS_GUARD:
        lock = _PARTICLE_LOCKS.setdefault(dest, threading.Lock())
    with lock:
        if dest.exists():  # another job finished this exact render while we waited
            return dest
        return render_animation("particles", {}, seconds, dest, width, height, fps)


def render_animation(template: str, params: Dict[str, Any], duration: float,
                     dest: Path, width: int, height: int, fps: int,
                     theme: str = "classic") -> Path:
    """Render one animation beat to a silent MP4 of exactly `duration` s."""
    from playwright.sync_api import sync_playwright

    if not (config.ANIM_DIR / f"{template}.html").exists():
        raise FileNotFoundError(f"unknown animation template: {template}")
    tpl = _themed_template(template, theme, dest.parent)

    frames_dir = dest.parent / f"{dest.stem}_frames"
    frames_dir.mkdir(exist_ok=True)
    n_frames = max(1, round(duration * fps))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(tpl.as_uri())
        page.evaluate("([p, d]) => setup(p, d)", [params, float(duration)])
        for i in range(n_frames):
            page.evaluate("t => seek(t)", i / fps)
            page.screenshot(path=str(frames_dir / f"f_{i:05d}.png"))
        browser.close()

    p = subprocess.run(
        ["ffmpeg", "-y", "-framerate", str(fps),
         "-i", str(frames_dir / "f_%05d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
        capture_output=True, text=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    if tpl.name.startswith("_"):  # themed temp copy
        tpl.unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg frame assembly failed:\n" + p.stderr[-800:])
    return dest
