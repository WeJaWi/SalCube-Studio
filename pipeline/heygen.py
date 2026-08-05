"""HeyGen avatar clips for the on-camera host (A-roll + split-screens).

The user's own HeyGen avatar speaks segments of the ai33 narration: we pass
the narration audio by URL (voice type "audio"), so the voice is identical
whether Hannah is on camera or narrating b-roll. Only the visible segments
are generated — HeyGen credits are the budget, enforced by the planner.

Submission and collection are split so all clips render in parallel on
HeyGen's side. If HeyGen can't fetch the ai33 audio URL, the clip is
retried once with the audio uploaded to HeyGen's asset store.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import requests

from . import clients, config


def available() -> bool:
    return bool(config.HEYGEN_API_KEY and config.HEYGEN_AVATAR_ID)


def _headers() -> dict:
    return {"X-Api-Key": config.HEYGEN_API_KEY, "Content-Type": "application/json"}


def upload_audio_asset(path: Path) -> str:
    """Upload a local audio file to HeyGen's asset store; returns asset id."""
    with open(path, "rb") as f:
        r = requests.post(
            f"{config.HEYGEN_UPLOAD_URL}/v1/asset",
            headers={"X-Api-Key": config.HEYGEN_API_KEY,
                     "Content-Type": "audio/mpeg"},
            data=f.read(), timeout=300)
    data = r.json()
    asset = (data.get("data") or {})
    if r.status_code >= 400 or not asset.get("id"):
        raise RuntimeError(f"heygen asset upload error {r.status_code}: "
                           f"{str(data)[:300]}")
    return asset["id"]


def submit_talking(audio_url: Optional[str] = None,
                   audio_asset_id: Optional[str] = None,
                   width: int = 1920, height: int = 1080,
                   avatar_id: Optional[str] = None,
                   avatar_type: Optional[str] = None) -> str:
    """Kick off one avatar clip; returns HeyGen video_id.

    avatar_id/avatar_type override the .env defaults (profiles carry their
    own host character)."""
    avatar_id = avatar_id or config.HEYGEN_AVATAR_ID
    avatar_type = avatar_type or config.HEYGEN_AVATAR_TYPE
    if not (config.HEYGEN_API_KEY and avatar_id):
        raise RuntimeError("HeyGen key / avatar id not configured")
    voice: dict = {"type": "audio"}
    if audio_asset_id:
        voice["audio_asset_id"] = audio_asset_id
    elif audio_url:
        voice["audio_url"] = audio_url
    else:
        raise ValueError("audio_url or audio_asset_id required")

    if avatar_type == "talking_photo":
        character = {"type": "talking_photo", "talking_photo_id": avatar_id}
    else:
        character = {"type": "avatar", "avatar_id": avatar_id,
                     "avatar_style": config.HEYGEN_AVATAR_STYLE or "normal"}
    body = {
        "video_inputs": [{
            "character": character,
            "voice": voice,
        }],
        "dimension": {"width": width, "height": height},
    }
    r = requests.post(f"{config.HEYGEN_BASE_URL}/v2/video/generate",
                      headers=_headers(), json=body, timeout=60)
    data = r.json()
    vid = (data.get("data") or {}).get("video_id")
    if r.status_code >= 400 or not vid:
        raise RuntimeError(f"heygen generate error {r.status_code}: "
                           f"{data.get('error') or str(data)[:300]}")
    return vid


def collect_talking(video_id: str, dest: Path, audio_path: Optional[Path] = None,
                    width: int = 1920, height: int = 1080,
                    timeout_s: int = 2400, interval: float = 10.0,
                    avatar_id: Optional[str] = None,
                    avatar_type: Optional[str] = None,
                    log=lambda *_: None) -> Path:
    """Wait for a HeyGen clip and download it; one retry with uploaded audio."""
    try:
        url = _poll(video_id, timeout_s, interval)
    except RuntimeError as e:
        if not audio_path or not audio_path.exists():
            raise
        log(f"HeyGen retry with uploaded audio ({e})")
        asset_id = upload_audio_asset(audio_path)
        retry_id = submit_talking(audio_asset_id=asset_id, width=width,
                                  height=height, avatar_id=avatar_id,
                                  avatar_type=avatar_type)
        url = _poll(retry_id, timeout_s, interval)
    return clients.download(url, dest)


def _poll(video_id: str, timeout_s: int, interval: float) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{config.HEYGEN_BASE_URL}/v1/video_status.get",
                             headers={"X-Api-Key": config.HEYGEN_API_KEY},
                             params={"video_id": video_id}, timeout=60)
            data = (r.json().get("data") or {})
        except (requests.RequestException, ValueError):
            time.sleep(interval)  # transient — keep polling
            continue
        status = data.get("status")
        if status == "completed":
            url = data.get("video_url")
            if not url:
                raise RuntimeError("heygen: completed but no video_url")
            return url
        if status == "failed":
            err = data.get("error") or {}
            raise RuntimeError(f"heygen video failed: {err.get('message') or err}")
        time.sleep(interval)
    raise RuntimeError(f"heygen video {video_id} timed out after {timeout_s}s")


# ───────────────────── Avatar V (v3 platform) ─────────────────────
def submit_talking_v3(audio_asset_id: str, avatar_id: str,
                      resolution: str = "720p",
                      aspect_ratio: str = "16:9",
                      engine: str = "avatar_v") -> str:
    """Kick off one Avatar IV/V clip driven by uploaded audio; returns video_id.

    The v3 platform animates the whole photo (head, hands, posture) — far
    more realistic than the classic talking_photo engine, at a higher
    per-minute cost. 720p keeps that affordable; the renderer upscales."""
    r = requests.post(f"{config.HEYGEN_BASE_URL}/v3/videos", headers=_headers(),
                      json={"type": "avatar", "avatar_id": avatar_id,
                            "audio_asset_id": audio_asset_id,
                            "engine": {"type": engine},
                            "resolution": resolution,
                            "aspect_ratio": aspect_ratio}, timeout=120)
    data = r.json()
    vid = (data.get("data") or {}).get("video_id")
    if r.status_code >= 400 or not vid:
        raise RuntimeError(f"heygen v3 submit error {r.status_code}: "
                           f"{str(data)[:300]}")
    return vid


def collect_talking_v3(video_id: str, dest: Path, timeout_s: int = 2400,
                       interval: float = 10.0,
                       log=lambda *_: None) -> Path:
    """Wait for a v3 (Avatar IV/V) clip and download it."""
    waited = 0.0
    while waited < timeout_s:
        try:
            r = requests.get(f"{config.HEYGEN_BASE_URL}/v3/videos/{video_id}",
                             headers={"X-Api-Key": config.HEYGEN_API_KEY},
                             timeout=60)
            d = (r.json().get("data") or {})
        except (requests.RequestException, ValueError):
            time.sleep(interval)
            waited += interval
            continue
        status = d.get("status")
        if status == "completed" and d.get("video_url"):
            media = requests.get(d["video_url"], timeout=600)
            media.raise_for_status()
            dest.write_bytes(media.content)
            return dest
        if status == "failed":
            raise RuntimeError(f"heygen v3 video failed: "
                               f"{d.get('failure_message') or 'unknown'}")
        time.sleep(interval)
        waited += interval
    raise RuntimeError(f"heygen v3 video {video_id} timed out")
