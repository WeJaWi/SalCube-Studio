"""AI b-roll clips via a kie text-to-video model.

Default is Grok Imagine — the cheapest option on kie — which takes an exact
duration (6-30s in 1s steps), so clips match the narration length instead of
looping. Other model families (bytedance/seedance, wan, kling) are supported
via KIE_VIDEO_MODEL in .env; their fixed-length clips get looped by the
render. Submit/collect are split so all clips generate in parallel.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import clients, config


def _input_for(model: str, prompt: str, duration_s: float, resolution: str) -> Dict[str, Any]:
    if model.startswith("grok-imagine"):
        return {
            "prompt": prompt[:2000],
            "duration": max(6, min(30, round(duration_s))),
            "aspect_ratio": "16:9",
            "resolution": resolution if resolution in ("480p", "720p") else "720p",
            "mode": "normal",
            "nsfw_checker": False,
        }
    if model.startswith("wan/"):
        return {
            "prompt": prompt[:2000],
            "aspect_ratio": "16:9",
            "resolution": resolution if resolution in ("480p", "720p") else "720p",
            "nsfw_checker": False,
        }
    # bytedance/seedance, kling, and similar 5s/10s families
    return {
        "prompt": prompt[:2000],
        "duration": "10" if duration_s > 6 else "5",
        "aspect_ratio": "16:9",
        "resolution": resolution,
        "nsfw_checker": False,
    }


def submit_clip(prompt: str, duration_s: float, resolution: str = "720p",
                *, context: str = "") -> str:
    """Kick off one b-roll generation; returns the kie task id."""
    model = config.KIE_VIDEO_MODEL
    return clients.kie_create_task(model, _input_for(model, prompt, duration_s, resolution),
                                   api_key=config.KIE_VIDEO_API_KEY,
                                   context=context, key_label="video")


def collect_clip(task_id: str, dest: Path, timeout_s: int = 1200,
                 *, context: str = "") -> Path:
    urls = clients.kie_poll(task_id, timeout_s=timeout_s,
                            api_key=config.KIE_VIDEO_API_KEY,
                            model=config.KIE_VIDEO_MODEL, context=context,
                            key_label="video")
    if not urls:
        raise RuntimeError("video model returned no URL")
    return clients.download(urls[0], dest)
