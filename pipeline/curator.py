"""LLM curator: judges stock candidates against the narration they'll cover.

"text" mode compares candidate metadata to the narration; "vision" mode
additionally downloads each candidate's thumbnail so the model looks at the
actual pictures before choosing. Judging always runs on the Claude endpoint
regardless of the script-model dropdown — thumbnails are tiny, so a whole
video's worth of judging costs well under a dollar, and it must keep working
mid-render even when a GPT model wrote the script.

choose() returns an index into the candidate list, -1 when nothing genuinely
fits (caller falls back to photo → AI image), or None when the judge itself
failed (caller degrades to keyword scoring — a render never dies here).
"""
from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import requests

from . import config

MAX_CANDIDATES = 8

_RULES = (
    'Pick the ONE candidate whose actual visual content best matches what '
    'the narration is about — the subject and mood of the whole passage, '
    'not just shared words.\n'
    'Rules:\n'
    '- Prefer real live-action photography/footage. Reject cartoons, 3D '
    'renders, illustrations and stylized graphics unless the narration is '
    'about something abstract that only they could show.\n'
    '- Reject anything with big burned-in text, logos or watermarks.\n'
    '- A generic-but-relevant shot beats a specific-but-wrong one.\n'
    '- If NOTHING genuinely fits the narration, answer -1 — a fallback '
    'visual will be generated instead, which beats a mismatched clip.\n'
    'Reply with ONLY JSON: {"pick": <candidate number or -1>}'
)


def _fetch_thumb(url: str) -> Optional[dict]:
    """Download one preview image as an Anthropic image block, or None."""
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        media_type = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        if not media_type.startswith("image/"):
            media_type = "image/jpeg"
        return {"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.b64encode(r.content).decode()}}
    except Exception:  # noqa: BLE001
        return None


def _ask(content: list, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{config.LLM_BASE_URL}{config.LLM_CHAT_PATH}",
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": config.LLM_MODEL,
                      "messages": [{"role": "user", "content": content}],
                      "max_tokens": 200},
                timeout=120)
            if r.status_code == 200:
                text = "".join(b.get("text", "")
                               for b in r.json().get("content", []) or []
                               if b.get("type") == "text")
                s, e = text.find("{"), text.rfind("}")
                if s != -1 and e != -1:
                    return json.loads(text[s:e + 1])
        except Exception:  # noqa: BLE001
            pass
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    return None


def choose(narration: str, query: str, hits: List, mode: str) -> Optional[int]:
    """Judge candidates against the narration. See module docstring."""
    if not hits:
        return None
    hits = hits[:MAX_CANDIDATES]

    content: list = [{"type": "text", "text": (
        "You are curating b-roll for a documentary-style YouTube video.\n"
        f"NARRATION this shot must cover:\n\"{narration[:700]}\"\n"
        f"(planned search query was: \"{query}\")\n\n"
        f"{len(hits)} candidates follow, numbered from 0.")}]

    thumbs: List[Optional[dict]] = [None] * len(hits)
    if mode == "vision":
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {i: ex.submit(_fetch_thumb, h.thumb)
                       for i, h in enumerate(hits) if h.thumb}
            for i, f in futures.items():
                thumbs[i] = f.result()

    for i, h in enumerate(hits):
        dur = f", {h.duration}s" if h.duration else ""
        desc = (h.meta or "no description")[:160]
        note = "" if thumbs[i] else (
            " (no preview available — judge by description)"
            if mode == "vision" else "")
        content.append({"type": "text",
                        "text": f"Candidate {i} ({h.kind}{dur}): {desc}{note}"})
        if thumbs[i]:
            content.append(thumbs[i])

    content.append({"type": "text", "text": _RULES})

    data = _ask(content)
    if not isinstance(data, dict):
        return None
    try:
        pick = int(data.get("pick"))
    except (TypeError, ValueError):
        return None
    if pick == -1:
        return -1
    return pick if 0 <= pick < len(hits) else None
