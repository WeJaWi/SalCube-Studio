"""AI image generation via kie.ai Qwen Z-Image."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import clients, config

# kie z-image supports: 1:1, 4:3, 3:4, 16:9, 9:16
_ASPECT = {(16, 9): "16:9", (9, 16): "9:16", (4, 3): "4:3", (3, 4): "3:4", (1, 1): "1:1"}


def _aspect_for(width: int, height: int) -> str:
    from math import gcd
    g = gcd(width, height) or 1
    key = (width // g, height // g)
    return _ASPECT.get(key, "16:9")


def submit_grok_image(prompt: str, *, context: str = "") -> str:
    """Kick off a cheap Grok Imagine still (uses the grok-authorized key)."""
    return clients.kie_create_task("grok-imagine/text-to-image", {
        "prompt": prompt[:2000],
        "aspect_ratio": "16:9",
        "nsfw_checker": False,
    }, api_key=config.KIE_VIDEO_API_KEY, context=context, key_label="video")


def collect_grok_image(task_id: str, dest: Path, timeout_s: int = 600,
                       *, context: str = "") -> Path:
    urls = clients.kie_poll(task_id, timeout_s=timeout_s,
                            api_key=config.KIE_VIDEO_API_KEY,
                            model="grok-imagine/text-to-image", context=context,
                            key_label="video")
    if not urls:
        raise RuntimeError("grok image returned no URL")
    return clients.download(urls[0], dest)


def submit_image(prompt: str, width: int, height: int,
                 *, context: str = "") -> str:
    """Submit one Z-Image task so its ID can be persisted before polling."""
    if not config.KIE_API_KEY:
        raise RuntimeError("KIE_API_KEY is not set")
    return clients.kie_create_task(config.KIE_IMAGE_MODEL, {
        "prompt": prompt[:1000],
        "aspect_ratio": _aspect_for(width, height),
        "nsfw_checker": False,
    }, context=context, key_label="main")


def collect_image(task_id: str, dest: Path, *, context: str = "") -> Path:
    """Collect a previously submitted Z-Image task without resubmitting it."""
    urls = clients.kie_poll(task_id, model=config.KIE_IMAGE_MODEL,
                            context=context, key_label="main")
    if not urls:
        raise RuntimeError("kie z-image returned no image URL")
    return clients.download(urls[0], dest)


def generate_image(prompt: str, dest: Path, width: int, height: int,
                   *, context: str = "") -> Path:
    """Generate one Z-Image for `prompt`, save to `dest`."""
    task_id = submit_image(prompt, width, height, context=context)
    return collect_image(task_id, dest, context=context)
