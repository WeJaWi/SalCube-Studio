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


def submit_grok_image(prompt: str) -> str:
    """Kick off a cheap Grok Imagine still (uses the grok-authorized key)."""
    return clients.kie_create_task("grok-imagine/text-to-image", {
        "prompt": prompt[:2000],
        "aspect_ratio": "16:9",
        "nsfw_checker": False,
    }, api_key=config.KIE_VIDEO_API_KEY)


def collect_grok_image(task_id: str, dest: Path, timeout_s: int = 600) -> Path:
    urls = clients.kie_poll(task_id, timeout_s=timeout_s,
                            api_key=config.KIE_VIDEO_API_KEY)
    if not urls:
        raise RuntimeError("grok image returned no URL")
    return clients.download(urls[0], dest)


def generate_image(prompt: str, dest: Path, width: int, height: int) -> Path:
    """Generate one Z-Image for `prompt`, save to `dest`."""
    if not config.KIE_API_KEY:
        raise RuntimeError("KIE_API_KEY is not set")
    task_id = clients.kie_create_task(config.KIE_IMAGE_MODEL, {
        "prompt": prompt[:1000],
        "aspect_ratio": _aspect_for(width, height),
        "nsfw_checker": False,
    })
    urls = clients.kie_poll(task_id)
    if not urls:
        raise RuntimeError("kie z-image returned no image URL")
    return clients.download(urls[0], dest)
