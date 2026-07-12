"""Central configuration: loads .env and exposes typed getters."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "profiles"
OUTPUT_DIR = ROOT / "output"
HOSTS_DIR = ROOT / "hosts"
ANIM_DIR = ROOT / "anim_templates"
OUTPUT_DIR.mkdir(exist_ok=True)
HOSTS_DIR.mkdir(exist_ok=True)


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ── LLM / script ────────────────────────────────────────────────
LLM_API_KEY = get("LLM_API_KEY")
LLM_BASE_URL = get("LLM_BASE_URL").rstrip("/")
LLM_CHAT_PATH = get("LLM_CHAT_PATH", "/v1/chat/completions")
LLM_MODEL = get("LLM_MODEL", "claude-sonnet-5")

# ── GPT 5.6 via kie (alternative script model) ──────────────────
LLM_GPT_API_KEY = get("LLM_GPT_API_KEY")

# ── kie.ai (images, avatar A-roll, AI b-roll) ───────────────────
KIE_API_KEY = get("KIE_API_KEY")
KIE_BASE_URL = get("KIE_BASE_URL", "https://api.kie.ai").rstrip("/")
KIE_UPLOAD_BASE_URL = get("KIE_UPLOAD_BASE_URL", "https://kieai.redpandaai.co").rstrip("/")
KIE_IMAGE_MODEL = get("KIE_IMAGE_MODEL", "z-image")
KIE_VIDEO_MODEL = get("KIE_VIDEO_MODEL", "grok-imagine/text-to-video")
# dedicated key for the video model (falls back to the main kie key)
KIE_VIDEO_API_KEY = get("KIE_VIDEO_API_KEY") or get("KIE_API_KEY")

# ── HeyGen (the on-camera host) ─────────────────────────────────
HEYGEN_API_KEY = get("HEYGEN_API_KEY")
HEYGEN_AVATAR_ID = get("HEYGEN_AVATAR_ID")
HEYGEN_AVATAR_TYPE = get("HEYGEN_AVATAR_TYPE", "avatar")  # avatar | talking_photo
HEYGEN_AVATAR_STYLE = get("HEYGEN_AVATAR_STYLE", "normal")
HEYGEN_BASE_URL = get("HEYGEN_BASE_URL", "https://api.heygen.com").rstrip("/")
HEYGEN_UPLOAD_URL = get("HEYGEN_UPLOAD_URL", "https://upload.heygen.com").rstrip("/")

# ── ai33.pro (music, voice, subtitles) ──────────────────────────
AI33_API_KEY = get("AI33_API_KEY")
AI33_BASE_URL = get("AI33_BASE_URL", "https://api.ai33.pro").rstrip("/")
AI33_VOICE_ID = get("AI33_VOICE_ID", "elevenlabs_21m00Tcm4TlvDq8ikWAM")
AI33_TTS_SPEED = get("AI33_TTS_SPEED", "1")

# ── Pixabay + Pexels (stock) ────────────────────────────────────
PIXABAY_API_KEY = get("PIXABAY_API_KEY")
PEXELS_API_KEY = get("PEXELS_API_KEY")
# separate key for image search (spreads the per-key rate limit)
PIXABAY_IMAGE_API_KEY = get("PIXABAY_IMAGE_API_KEY") or get("PIXABAY_API_KEY")

# ── Captions ────────────────────────────────────────────────────
CAPTION_FONT_FILE = get("CAPTION_FONT_FILE")


# Script models the UI can pick from ("format" selects payload/parse shape)
SCRIPT_MODELS = {
    "sonnet-5": {"format": "anthropic", "base": LLM_BASE_URL,
                 "path": LLM_CHAT_PATH, "model": LLM_MODEL, "key": LLM_API_KEY},
    "gpt-5.6-terra": {"format": "responses", "base": KIE_BASE_URL,
                      "path": "/codex/v1/responses", "model": "gpt-5-6-terra",
                      "key": LLM_GPT_API_KEY},
    "gpt-5.6-luna": {"format": "responses", "base": KIE_BASE_URL,
                     "path": "/codex/v1/responses", "model": "gpt-5-6-luna",
                     "key": LLM_GPT_API_KEY},
    "gpt-5.6-sol": {"format": "responses", "base": KIE_BASE_URL,
                    "path": "/codex/v1/responses", "model": "gpt-5-6-sol",
                    "key": LLM_GPT_API_KEY},
}


def missing_keys() -> list[str]:
    required = {
        "LLM_API_KEY": LLM_API_KEY,
        "LLM_BASE_URL": LLM_BASE_URL,
        "KIE_API_KEY": KIE_API_KEY,
        "AI33_API_KEY": AI33_API_KEY,
        "PIXABAY_API_KEY": PIXABAY_API_KEY,
    }
    return [k for k, v in required.items() if not v]


def find_font() -> str:
    if CAPTION_FONT_FILE and Path(CAPTION_FONT_FILE).exists():
        return CAPTION_FONT_FILE
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


# ── Multi-user access (remote use over Tailscale etc.) ─────────
# ACCESS_KEYS="north:abc123:*, friend:xyz789:matthias-richter"
# name : secret key : allowed profiles ("|"-separated, "*" = all).
# When unset, the app runs open on localhost only (single-user mode).
def access_keys() -> list:
    raw = get("ACCESS_KEYS", "")
    out = []
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) != 3 or not parts[1]:
            continue
        name, key, profs = (p.strip() for p in parts)
        out.append({"name": name, "key": key,
                    "profiles": None if profs == "*"
                    else {p.strip() for p in profs.split("|") if p.strip()}})
    return out
