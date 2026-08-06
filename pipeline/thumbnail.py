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

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import clients, config


def _authentication_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in (
        "unauthorized", "authentication failed", "not authorized",
        "invalid api key", "permission denied", "access denied",
        "do not have access", "not permitted", "error 401", "error 403",
    ))


def build_prompt(main_text: str, red_word: str, yellow_word: str,
                 name: str, *, no_character: bool = False) -> str:
    """The edit instruction sent to GPT Image 2 alongside the two images."""
    character_instruction = (
        "Remove every person and character from the composition. Replace the "
        "portrait area with an elegant symbolic visual matching the topic."
        if no_character else
        "Change the character to the character shown in image 2."
    )
    return (
        f'Keep the original quality, color scheme, font, and textures of '
        f'image 1, but {character_instruction} '
        f'Change the headline text to "{main_text}", put the '
        f'word "{red_word}" in the red box, and put the word '
        f'"{yellow_word}" in the yellowish font. The name at the bottom '
        f'will be "{name}" instead of the other one, in the same '
        f'yellowish/gold font as the original.'
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = config.find_font()
    if path:
        return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize and center-crop an image without changing its proportions."""
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
          max_width: int) -> list[str]:
    """Word-wrap while also surviving a single unusually long word."""
    words = " ".join(str(text).split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
            current = ""
        # Character-wrap a word that is wider than the entire text column.
        piece = ""
        for char in word:
            trial_piece = piece + char
            if piece and draw.textbbox((0, 0), trial_piece, font=font)[2] > max_width:
                lines.append(piece)
                piece = char
            else:
                piece = trial_piece
        current = piece
    if current:
        lines.append(current)
    return lines or [""]


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int,
              max_height: int, *, largest: int, smallest: int,
              max_lines: int) -> tuple[ImageFont.ImageFont, str, int]:
    for size in range(largest, smallest - 1, -2):
        font = _font(size)
        lines = _wrap(draw, text, font, max_width)
        spacing = max(6, size // 8)
        joined = "\n".join(lines)
        box = draw.multiline_textbbox((0, 0), joined, font=font,
                                      spacing=spacing, stroke_width=2)
        if len(lines) <= max_lines and box[3] - box[1] <= max_height:
            return font, joined, spacing
    font = _font(smallest)
    lines = _wrap(draw, text, font, max_width)[:max_lines]
    return font, "\n".join(lines), max(5, smallest // 8)


def _paste_character(canvas: Image.Image, character_path: Path) -> None:
    character = Image.open(character_path)
    if "A" in character.getbands():
        person = character.convert("RGBA")
        person.thumbnail((520, 670), Image.Resampling.LANCZOS)
        x = max(0, (500 - person.width) // 2)
        y = 720 - person.height
        alpha = person.getchannel("A")
        glow_alpha = alpha.filter(ImageFilter.GaussianBlur(24)).point(
            lambda value: int(value * 0.45))
        glow = Image.new("RGBA", person.size, (215, 158, 60, 0))
        glow.putalpha(glow_alpha)
        canvas.alpha_composite(glow, (x, y))
        canvas.alpha_composite(person, (x, y))
        return

    # Host photographs do not always have transparency. Crop them into the
    # portrait side and feather the right edge into the text background.
    portrait = _cover(character.convert("RGB"), (535, 720)).convert("RGBA")
    mask = Image.new("L", portrait.size, 255)
    mask_draw = ImageDraw.Draw(mask)
    for x in range(405, 535):
        opacity = int(255 * (535 - x) / 130)
        mask_draw.line((x, 0, x, 719), fill=opacity)
    portrait.putalpha(mask)
    canvas.alpha_composite(portrait, (0, 0))


def _draw_topic_symbol(canvas: Image.Image, name: str) -> None:
    """Simple character-free mark for Reddit/Astrology profile thumbnails."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    lowered = name.lower()
    if "astro" in lowered:
        # Crescent + sparse star field; no fake zodiac claims or readable text.
        draw.ellipse((125, 150, 420, 445), fill=(231, 181, 75, 230))
        draw.ellipse((205, 105, 475, 405), fill=(24, 18, 28, 255))
        for x, y, r in ((92, 110, 5), (340, 92, 7), (438, 210, 4),
                        (105, 520, 6), (378, 535, 5), (250, 80, 3)):
            draw.ellipse((x-r, y-r, x+r, y+r), fill=(248, 222, 151, 230))
    else:
        # Layered conversation cards evoke an online story without a logo.
        for offset, alpha in ((38, 95), (19, 155), (0, 235)):
            box = (82 + offset, 175 - offset, 426 + offset, 405 - offset)
            draw.rounded_rectangle(box, radius=26,
                                   fill=(242, 237, 225, alpha),
                                   outline=(225, 173, 72, alpha), width=4)
        for y, frac in ((230, .72), (276, .58), (322, .66)):
            draw.rounded_rectangle((130, y, 130 + int(250 * frac), y + 15),
                                   radius=7, fill=(77, 54, 46, 210))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.4)))


def generate_local(template_path: Path, character_path: Optional[Path], dest: Path, *,
                   main_text: str, red_word: str, yellow_word: str,
                   name: str) -> Path:
    """Create a deterministic thumbnail when no remote image model is usable.

    The template supplies the color/texture family while the compositor owns
    the layout, so old template wording can never leak into the result.
    """
    base = _cover(Image.open(template_path).convert("RGB"), (1280, 720))
    base = base.filter(ImageFilter.GaussianBlur(18))
    base = ImageEnhance.Color(base).enhance(0.72).convert("RGBA")
    canvas = Image.new("RGBA", (1280, 720), (20, 15, 12, 255))
    canvas.alpha_composite(base)

    # Hide the original template composition but retain its warm visual grain.
    shade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for x in range(1280):
        # The text side is darkest; the portrait side remains gently visible.
        alpha = int(118 + (x / 1279) * 105)
        shade_draw.line((x, 0, x, 719), fill=(12, 8, 7, alpha))
    canvas.alpha_composite(shade)
    if character_path is not None:
        _paste_character(canvas, character_path)
    else:
        _draw_topic_symbol(canvas, name)

    # A dark glass text panel keeps every language readable over any template.
    panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rounded_rectangle((475, 32, 1254, 688), radius=28,
                                 fill=(10, 8, 8, 196),
                                 outline=(219, 170, 76, 72), width=2)
    canvas.alpha_composite(panel)
    draw = ImageDraw.Draw(canvas)

    left, width = 515, 700
    main_font, main_lines, spacing = _fit_text(
        draw, main_text, width, 305, largest=92, smallest=42, max_lines=4)
    draw.multiline_text((left + 5, 66 + 7), main_lines, font=main_font,
                        fill=(0, 0, 0, 205), spacing=spacing, align="left")
    draw.multiline_text((left, 66), main_lines, font=main_font,
                        fill=(248, 246, 239, 255), spacing=spacing,
                        stroke_width=2, stroke_fill=(20, 14, 12, 255))
    main_box = draw.multiline_textbbox((left, 66), main_lines, font=main_font,
                                       spacing=spacing, stroke_width=2)
    y = max(360, main_box[3] + 35)

    red_font, red_lines, red_spacing = _fit_text(
        draw, red_word, 470, 90, largest=62, smallest=34, max_lines=2)
    red_box = draw.multiline_textbbox((0, 0), red_lines, font=red_font,
                                      spacing=red_spacing, stroke_width=1)
    red_w = min(width, red_box[2] - red_box[0] + 48)
    red_h = red_box[3] - red_box[1] + 26
    draw.rounded_rectangle((left, y, left + red_w, y + red_h), radius=12,
                           fill=(188, 31, 35, 255))
    draw.multiline_text((left + 24, y + 8 - red_box[1]), red_lines,
                        font=red_font, fill=(255, 255, 255, 255),
                        spacing=red_spacing, stroke_width=1,
                        stroke_fill=(105, 8, 12, 255))
    y += red_h + 22

    yellow_font, yellow_lines, yellow_spacing = _fit_text(
        draw, yellow_word, width, 90, largest=58, smallest=32, max_lines=2)
    draw.multiline_text((left, y), yellow_lines, font=yellow_font,
                        fill=(238, 190, 83, 255), spacing=yellow_spacing,
                        stroke_width=2, stroke_fill=(36, 22, 10, 255))

    name_font, name_lines, name_spacing = _fit_text(
        draw, name, width, 52, largest=38, smallest=24, max_lines=1)
    name_box = draw.multiline_textbbox((0, 0), name_lines, font=name_font,
                                       spacing=name_spacing)
    name_x = 1215 - (name_box[2] - name_box[0])
    draw.line((left, 626, 1215, 626), fill=(219, 170, 76, 135), width=2)
    draw.text((name_x, 642), name_lines, font=name_font,
              fill=(224, 174, 76, 255), stroke_width=1,
              stroke_fill=(30, 17, 8, 255))

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dest, "PNG", optimize=True)
    return dest


def generate(template_path: Path, character_path: Optional[Path], dest: Path, *,
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
        return generate_local(
            template_path, character_path, dest, main_text=main_text,
            red_word=red_word, yellow_word=yellow_word, name=name)
    dedicated_key = config.KIE_THUMBNAIL_API_KEY
    # Kie's upload gateway can reject a model-scoped/dedicated key even when
    # that key is valid for createTask. The main Kie key is the verified
    # transport credential; generated URLs remain short-lived and private.
    upload_key = config.KIE_API_KEY or dedicated_key
    try:
        template_url = clients.kie_upload(
            template_path, "thumbnails", api_key=upload_key)
        character_url = (clients.kie_upload(
            character_path, "thumbnails", api_key=upload_key)
            if character_path is not None else None)
    except Exception as exc:  # noqa: BLE001
        if _authentication_failure(exc):
            return generate_local(
                template_path, character_path, dest, main_text=main_text,
                red_word=red_word, yellow_word=yellow_word, name=name)
        raise
    primary_model = model or config.KIE_THUMBNAIL_MODEL
    payload = {
        "prompt": build_prompt(main_text, red_word, yellow_word, name,
                               no_character=character_path is None),
        "input_urls": [url for url in (template_url, character_url) if url],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution or config.KIE_THUMBNAIL_RESOLUTION,
    }
    task_attempts = [(primary_model, dedicated_key, payload)]
    if config.KIE_API_KEY and config.KIE_API_KEY != dedicated_key:
        task_attempts.append((primary_model, config.KIE_API_KEY, payload))
    # The fleet already has a separately authorized Grok media key. Its
    # image-to-image endpoint is a safe final fallback when the configured
    # GPT Image model is not enabled for either image key.
    if config.KIE_VIDEO_API_KEY:
        grok_payload = {
            "prompt": build_prompt(main_text, red_word, yellow_word, name,
                                   no_character=character_path is None),
            "image_urls": [url for url in (template_url, character_url) if url],
        }
        candidate = ("grok-imagine/image-to-image",
                     config.KIE_VIDEO_API_KEY, grok_payload)
        if not any(m == candidate[0] and k == candidate[1]
                   for m, k, _ in task_attempts):
            task_attempts.append(candidate)
    last_error: Exception = RuntimeError("thumbnail task was not attempted")
    for task_model, key, task_payload in task_attempts:
        try:
            task_id = clients.kie_create_task(
                task_model, task_payload, api_key=key)
            urls = clients.kie_poll(task_id, timeout_s=timeout_s, api_key=key)
            if not urls:
                raise RuntimeError("thumbnail model returned no image URL")
            return clients.download(urls[0], dest)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _authentication_failure(exc):
                raise
    # Every remote key/model combination was rejected. Thumbnail production
    # must not block the video queue, so use the built-in compositor.
    if _authentication_failure(last_error):
        return generate_local(
            template_path, character_path, dest, main_text=main_text,
            red_word=red_word, yellow_word=yellow_word, name=name)
    raise last_error
