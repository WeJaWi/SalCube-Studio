"""Voiceover + subtitles via ai33.pro /v3/text-to-speech.

with_transcript=true makes ai33 return a ready-made .srt subtitle file
(metadata.srt_url) alongside the audio, which we burn into the video.

Returns (audio_path, srt_path, audio_url). The remote audio_url is kept
because kie's avatar models take the narration by URL — chaining the two
providers without re-uploading. srt_path/audio_url may be None.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from . import clients, config


def generate_voiceover(
    text: str, dest: Path, voice_id: Optional[str] = None,
    with_transcript: bool = True, timeout_s: int = 1800,
) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Synthesize `text`; save MP3 to `dest` and .srt next to it."""
    text = (text or "").strip()
    if not text or not config.AI33_API_KEY:
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
