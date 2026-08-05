"""Standalone YouTube-thumbnail generator — independent of the video pipeline.

Takes a template thumbnail (image 1: the style/colors/fonts/textures to
keep) and a character reference photo (image 2), and asks GPT Image 2 (via
kie.ai) to recompose the template with the new character and new text.

Uses its own kie.ai API key (KIE_THUMBNAIL_API_KEY) so this tool's spend is
tracked separately from the video pipeline's kie credits (HeyGen/TTS/stock
b-roll/video-gen all use KIE_API_KEY / KIE_VIDEO_API_KEY instead).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import clients, config


def build_prompt(main_text: str, red_word: str, yellow_word: str,
                 name: str) -> str:
    """The edit instruction sent to GPT Image 2 alongside the two images."""
    return (
        f'Keep the original quality, color scheme, font, and textures of '
        f'image 1, but change the character to the character shown in '
        f'image 2. Change the headline text to "{main_text}", put the '
        f'word "{red_word}" in the red box, and put the word '
        f'"{yellow_word}" in the yellowish font. The name at the bottom '
        f'will be "{name}" instead of the other one, in the same '
        f'yellowish/gold font as the original.'
    )


def generate(template_path: Path, character_path: Path, dest: Path, *,
            main_text: str, red_word: str, yellow_word: str, name: str,
            aspect_ratio: str = "16:9", resolution: Optional[str] = None,
            model: Optional[str] = None, timeout_s: int = 300) -> Path:
    """Generate one thumbnail from a template image + a character reference.

    Both images are uploaded to kie's temp storage first — the model needs
    public URLs, not local file paths. `model` overrides KIE_THUMBNAIL_MODEL
    per-call, for trying alternate kie.ai models (e.g. nano-banana) without
    an .env edit.
    """
    if not config.KIE_THUMBNAIL_API_KEY:
        raise RuntimeError("KIE_THUMBNAIL_API_KEY is not set")
    key = config.KIE_THUMBNAIL_API_KEY

    template_url = clients.kie_upload(template_path, "thumbnails", api_key=key)
    character_url = clients.kie_upload(character_path, "thumbnails", api_key=key)

    task_id = clients.kie_create_task(model or config.KIE_THUMBNAIL_MODEL, {
        "prompt": build_prompt(main_text, red_word, yellow_word, name),
        "input_urls": [template_url, character_url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution or config.KIE_THUMBNAIL_RESOLUTION,
    }, api_key=key)

    urls = clients.kie_poll(task_id, timeout_s=timeout_s, api_key=key)
    if not urls:
        raise RuntimeError("thumbnail model returned no image URL")
    return clients.download(urls[0], dest)
