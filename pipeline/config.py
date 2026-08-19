"""Central configuration: loads .env and exposes typed getters."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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

# ── Live-togglable settings (survive restarts, unlike an in-memory flag) ──
# Small and human-editable, sibling to queue_state.json. gpu_encode/beat
# workers are read ONCE per job at job-start (see orchestrator.py) — never
# read live mid-render, so a toggle flip can't change behavior for an
# already-running job. Queue/scheduling settings (below) ARE read live by
# the scheduler on every dispatch decision, by design — that's the whole
# point of a "settings menu" vs. startup-time env config.
SETTINGS_FILE = OUTPUT_DIR / "settings.json"
_SETTINGS_LOCK = threading.Lock()

# Every key here is a GLOBAL default; a profile can override any of these
# under settings["profiles"][name][key] — see queue_setting(). Kept as a
# flat dict (not nested per-key metadata) so save_settings' deep-merge and
# the UI's "using global default" placeholder logic both stay simple: a
# key's absence from settings["queue"] means "use this default", not "use
# zero/False".
QUEUE_DEFAULTS: Dict[str, Any] = {
    "max_concurrent_jobs": None,       # None = defer to MAX_CONCURRENT_JOBS (env/auto)
    "max_planning_jobs": 2,
    "priority_mode": "automatic",      # automatic | always_biggest | always_smallest | submission_order | manual
    "night_window": {"start": "22:00", "end": "07:00"},
    "small_threshold_chars": 40000,
    "big_threshold_chars": 140000,
    "big_job_concurrency": 1,
    "big_job_prefers_hw_encode": True,
    "job_retry_limit": 2,
    # Wait before an automatic retry: the failures this catches are mostly
    # provider outages, where retrying instantly just spends an attempt on a
    # service that is still down.
    "retry_delay_seconds": 60,
    # Live kill switch: stop picking up NEW jobs, leave running ones alone.
    "queue_paused": False,
    "stage_stale_after_minutes": 90,
    "notify_on_complete": False,
    "notify_on_failure": True,
    "notify_webhook_url": "",
    "daily_spend_cap_usd": None,       # None/absent = no cap
}

_SETTINGS_VERSION = 2


def _migrate_settings(raw: dict) -> dict:
    """Upgrade whatever's on disk to the current schema. The only shape
    that ever existed before v2 is a bare {"gpu_encode": bool} — wrap it
    rather than lose the toggle a user already set."""
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("version") == _SETTINGS_VERSION:
        raw.setdefault("queue", {})
        raw.setdefault("profiles", {})
        return raw
    return {
        "version": _SETTINGS_VERSION,
        "gpu_encode": bool(raw.get("gpu_encode", False)),
        "queue": {},
        "profiles": {},
    }


def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
    except Exception:  # noqa: BLE001
        raw = {}
    return _migrate_settings(raw)


def save_settings(patch: dict) -> dict:
    """Deep-merge `patch` into the persisted settings and write atomically.

    A shallow dict.update() (the old behavior) would let a single-field
    POST like {"queue": {"max_planning_jobs": 3}} silently wipe every other
    key already set under "queue" — deep-merge only the two nested blocks
    that are ever partially patched; everything else stays a plain
    top-level overwrite, matching how `gpu_encode` already worked.
    """
    with _SETTINGS_LOCK:
        cur = load_settings()
        for key, val in patch.items():
            if key in ("queue", "profiles") and isinstance(val, dict):
                merged = dict(cur.get(key, {}))
                for pk, pv in val.items():
                    if key == "profiles" and isinstance(pv, dict):
                        merged[pk] = {**merged.get(pk, {}), **pv}
                    else:
                        merged[pk] = pv
                cur[key] = merged
            else:
                cur[key] = val
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=1))
        tmp.replace(SETTINGS_FILE)
        return cur


def queue_setting(key: str, profile: Optional[str] = None,
                  settings: Optional[dict] = None) -> Any:
    """Resolve one queue/scheduling setting: per-profile override, then the
    global value, then the hardcoded default. Pass an already-loaded
    `settings` dict when resolving several keys at once to avoid re-reading
    the file per key."""
    s = settings if settings is not None else load_settings()
    if profile:
        prof_val = (s.get("profiles", {}).get(profile, {}) or {}).get(key)
        if prof_val is not None:
            return prof_val
    q = s.get("queue", {}) or {}
    if key in q and q[key] is not None:
        return q[key]
    return QUEUE_DEFAULTS.get(key)


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name) or default)
    except ValueError:
        return default


def get_bool(name: str, default: bool) -> bool:
    raw = get(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ── Hardware detection — used by the auto-tuned concurrency settings
# below. No psutil dependency: os.cpu_count() plus one per-platform probe
# covers it. A render worker may be macOS, Windows, or Linux, and getting
# this wrong silently caps the machine at the 8GB fallback.
def _detect_cpu_count() -> int:
    return os.cpu_count() or 4


def _detect_ram_gb() -> float:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1e9
        if sys.platform == "win32":
            # ctypes, not `wmic` — wmic is deprecated and removed in
            # Windows 11 24H2, so shelling out to it silently fails there.
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1e9
        # Linux and anything else with a procfs
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / 1e9
    except Exception:  # noqa: BLE001
        pass
    return 8.0  # conservative fallback if the platform probe fails


def _auto_max_concurrent_jobs() -> int:
    """Job-level concurrency ceiling sized to this machine's real cores/RAM."""
    cores, ram_gb = _detect_cpu_count(), _detect_ram_gb()
    # ~3GB/job headroom (matches render.py's own per-process RSS ceiling),
    # plus 4GB reserved for the OS/other apps.
    by_ram = max(1, int((ram_gb - 4) // 3))
    by_cores = max(1, cores // 3)
    return max(1, min(by_ram, by_cores, 4))  # hard ceiling — untested above 4


def resolve_beat_workers(gpu_encode: bool) -> int:
    """How many beats one job composites in parallel. Read fresh per job
    (like resolve_model()) since it depends on gpu_encode, which can change
    between jobs via the settings toggle — never cache this at import time.
    """
    raw = get("BEAT_RENDER_WORKERS", "auto")
    if raw.lower() != "auto":
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cores = _detect_cpu_count()
    # A hardware encoder is a single shared block — concurrent sessions
    # don't scale the way parallel x264 threads do, so cap tighter with GPU
    # encode on than with plain CPU/x264 encode. The 2 is not arbitrary and
    # should not be "optimized" upward: consumer GeForce drivers also cap
    # simultaneous NVENC sessions (historically at 2), so a Windows worker
    # would start failing encodes above it.
    return 2 if gpu_encode else max(1, min(4, cores // 3))


_gpu_encoder_cache: Optional[str] = None

# Hardware H.264 encoders in preference order per platform. Every entry is
# H.264 — this app only ever produces H.264, and hardware vs software is
# the only axis that changes here.
_HW_ENCODERS = {
    "darwin": ["h264_videotoolbox"],
    "win32": ["h264_nvenc", "h264_amf", "h264_qsv"],
    "linux": ["h264_nvenc", "h264_qsv", "h264_vaapi"],
}


def gpu_encoder() -> str:
    """The hardware H.264 encoder ffmpeg on THIS machine actually has, e.g.
    "h264_videotoolbox" / "h264_nvenc" / "h264_qsv" — or "" for none.

    Real capability, not just "is the encoder name compiled into ffmpeg".
    A listed NVENC/QSV/AMF encoder can still be unusable when its driver or
    hardware is missing, so probe each candidate with a tiny real encode.
    Cached: encoder support can't change mid-process.
    Returning the name (rather than a bool) is what lets a fleet worker
    report what it has, since a Windows worker's encoder differs from a
    Mac's and the right ffmpeg flags differ with it.
    """
    global _gpu_encoder_cache
    if _gpu_encoder_cache is None:
        _gpu_encoder_cache = ""
        probe_args = {
            "h264_videotoolbox": [
                "-c:v", "h264_videotoolbox", "-b:v", "10M",
                "-maxrate", "14M",
            ],
            "h264_nvenc": [
                "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                "-cq", "23", "-b:v", "0",
            ],
            "h264_qsv": [
                "-c:v", "h264_qsv", "-global_quality", "23", "-b:v", "10M",
            ],
            "h264_amf": [
                "-c:v", "h264_amf", "-usage", "transcoding",
                "-quality", "balanced", "-b:v", "10M", "-maxrate", "14M",
            ],
            "h264_vaapi": ["-c:v", "h264_vaapi", "-qp", "23"],
        }
        common = [
            "-pix_fmt", "yuv420p", "-bf", "0",
            "-video_track_timescale", "15360",
        ]
        for name in _HW_ENCODERS.get(sys.platform, ["h264_nvenc"]):
            try:
                result = subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=640x360:d=0.4:r=30",
                    *probe_args[name], *common, "-f", "null", "-",
                ], capture_output=True, text=True, timeout=20)
                if result.returncode == 0:
                    _gpu_encoder_cache = name
                    break
            except Exception:  # noqa: BLE001
                continue
    return _gpu_encoder_cache


def gpu_encode_available() -> bool:
    """Back-compat wrapper — existing callers only care whether hardware
    encode is possible at all, not which chip provides it."""
    return bool(gpu_encoder())


# ── Render queue ─────────────────────────────────────────────────
# How many videos may render at once. Local compute (FFmpeg encode) is
# CPU-bound and scales with cores; the LLM/TTS/HeyGen/stock steps are all
# remote API calls, so raising this mostly trades CPU contention for
# throughput — watch provider rate limits too if you push it high.
# "auto" sizes it to this machine's detected cores/RAM; an explicit number
# in .env is always respected untouched.
_MAX_CONCURRENT_JOBS_RAW = get("MAX_CONCURRENT_JOBS", "1")
MAX_CONCURRENT_JOBS = (
    _auto_max_concurrent_jobs() if _MAX_CONCURRENT_JOBS_RAW.lower() == "auto"
    else get_int("MAX_CONCURRENT_JOBS", 1))


def max_concurrent_jobs() -> int:
    """Live-settable render-slot ceiling. A settings.json value (from the
    queue settings menu) overrides the startup env/auto value; clearing it
    (null) restores the original env-var behavior with no restart needed —
    MAX_CONCURRENT_JOBS itself is untouched so the startup banner and any
    other reader of the bare constant still see the env/auto value."""
    v = queue_setting("max_concurrent_jobs")
    return max(1, int(v)) if v else MAX_CONCURRENT_JOBS


def max_planning_jobs() -> int:
    """Live-settable ceiling on concurrent LLM-bound planning stages
    (outline/hooks/script) — separate from the render-slot ceiling above,
    since planning is API-bound (rate limits, spend) rather than CPU-bound."""
    return max(1, int(queue_setting("max_planning_jobs")))

# ── LLM / script ────────────────────────────────────────────────
LLM_API_KEY = get("LLM_API_KEY")
LLM_BASE_URL = get("LLM_BASE_URL").rstrip("/")
LLM_CHAT_PATH = get("LLM_CHAT_PATH", "/v1/chat/completions")
LLM_MODEL = get("LLM_MODEL", "claude-sonnet-5")

# ── GPT 5.6 via kie (alternative script model) ──────────────────
LLM_GPT_API_KEY = get("LLM_GPT_API_KEY")

# ── OpenRouter (read-only operations observer) ──────────────────────
OPENROUTER_API_KEY = get("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MONITOR_MODEL = get(
    "OPENROUTER_MONITOR_MODEL", "qwen/qwen3.8-27b")

# ── Grok 4.5 via kie (alternative script model) ──────────────────
# Own key so this model's spend is tracked apart from the others; falls
# back to the main kie key if not set separately.
LLM_GROK_API_KEY = get("LLM_GROK_API_KEY") or get("KIE_API_KEY")

# ── Claude CLI fallback (this machine's own Claude subscription) ──
# When kie.ai's script-model proxy is down/degraded (5xx after the normal
# retry budget in llm.chat() is exhausted), shell out to the locally
# installed `claude` CLI instead of failing the whole script section. Uses
# the user's own Claude Code login (subscription usage, not API-key
# billing) — no separate key needed here.
CLAUDE_CLI_FALLBACK = get_bool("CLAUDE_CLI_FALLBACK", True)
CLAUDE_CLI_PATH = get("CLAUDE_CLI_PATH", "claude")
CLAUDE_CLI_MODEL = get("CLAUDE_CLI_MODEL", "sonnet")
CLAUDE_CLI_TIMEOUT = get_int("CLAUDE_CLI_TIMEOUT", 300)

# ── kie.ai (images, avatar A-roll, AI b-roll) ───────────────────
KIE_API_KEY = get("KIE_API_KEY")
KIE_BASE_URL = get("KIE_BASE_URL", "https://api.kie.ai").rstrip("/")
KIE_UPLOAD_BASE_URL = get("KIE_UPLOAD_BASE_URL", "https://kieai.redpandaai.co").rstrip("/")
KIE_IMAGE_MODEL = get("KIE_IMAGE_MODEL", "z-image")
KIE_VIDEO_MODEL = get("KIE_VIDEO_MODEL", "grok-imagine/text-to-video")
# dedicated key for the video model (falls back to the main kie key)
KIE_VIDEO_API_KEY = get("KIE_VIDEO_API_KEY") or get("KIE_API_KEY")

# ── Thumbnail tool (standalone — separate from the video pipeline) ──
# Own API key so its spend is tracked apart from video-gen kie credits.
KIE_THUMBNAIL_API_KEY = get("KIE_THUMBNAIL_API_KEY") or get("KIE_API_KEY")
KIE_THUMBNAIL_MODEL = get("KIE_THUMBNAIL_MODEL", "gpt-image-2-image-to-image")
# gpt-image-2 has no "quality" field, only resolution (1K/2K/4K) — "medium"
# is mapped to 2K here; override via .env if a different tier is wanted.
KIE_THUMBNAIL_RESOLUTION = get("KIE_THUMBNAIL_RESOLUTION", "2K")

# ── HeyGen (the on-camera host) ─────────────────────────────────
HEYGEN_API_KEY = get("HEYGEN_API_KEY")
HEYGEN_AVATAR_ID = get("HEYGEN_AVATAR_ID")
HEYGEN_AVATAR_TYPE = get("HEYGEN_AVATAR_TYPE", "avatar")  # avatar | talking_photo
HEYGEN_AVATAR_STYLE = get("HEYGEN_AVATAR_STYLE", "normal")
HEYGEN_BASE_URL = get("HEYGEN_BASE_URL", "https://api.heygen.com").rstrip("/")
HEYGEN_UPLOAD_URL = get("HEYGEN_UPLOAD_URL", "https://upload.heygen.com").rstrip("/")

# ── ai33.pro (music, voice, subtitles) ──────────────────────────
AI33_API_KEY = get("AI33_API_KEY")
AI33_API_KEY_BACKUP = get("AI33_API_KEY_BACKUP")
AI33_BASE_URL = get("AI33_BASE_URL", "https://api.ai33.pro").rstrip("/")
AI33_VOICE_ID = get("AI33_VOICE_ID", "elevenlabs_21m00Tcm4TlvDq8ikWAM")
AI33_TTS_SPEED = get("AI33_TTS_SPEED", "1")
# Ceiling on characters per TTS call, grouping consecutive beats together
# up to this size instead of one call per beat (fewer calls -> fewer
# audible prosody-reset boundaries in the narration). Empirically tested
# against the real ai33.pro API (not guessed): a 40,054-char / ~40-minute
# single call succeeded with zero truncation, duration within 2% of
# expected. A 9,041-char call also succeeded cleanly.
AI33_MAX_TTS_CHARS = get_int("AI33_MAX_TTS_CHARS", 40000)

# ── Pixabay + Pexels (stock) ────────────────────────────────────
PIXABAY_API_KEY = get("PIXABAY_API_KEY")
PEXELS_API_KEY = get("PEXELS_API_KEY")
# separate key for image search (spreads the per-key rate limit)
PIXABAY_IMAGE_API_KEY = get("PIXABAY_IMAGE_API_KEY") or get("PIXABAY_API_KEY")

# ── Captions ────────────────────────────────────────────────────
CAPTION_FONT_FILE = get("CAPTION_FONT_FILE")

# ── Google Drive deliverable storage ──────────────────────────────
# The OAuth client JSON and refresh token are both local secrets.  The token
# is written under output/ (already gitignored) after one browser consent.
GDRIVE_CLIENT_SECRET_FILE = Path(get("GDRIVE_CLIENT_SECRET_FILE")) if get("GDRIVE_CLIENT_SECRET_FILE") else None
GDRIVE_TOKEN_FILE = Path(get("GDRIVE_TOKEN_FILE") or OUTPUT_DIR / "gdrive_token.json")
GDRIVE_FOLDER_NAME = get("GDRIVE_FOLDER_NAME", "videos")
GDRIVE_CHUNK_BYTES = max(256 * 1024, get_int("GDRIVE_CHUNK_MB", 8) * 1024 * 1024)
GDRIVE_REDIRECT_URI = get("GDRIVE_REDIRECT_URI")

# ── Render fleet (Tailscale transports the private HTTP connection) ─────
FLEET_ROLE = get("FLEET_ROLE", "off").lower()  # off | main | worker
FLEET_MAIN_URL = get("FLEET_MAIN_URL").rstrip("/")
FLEET_PAIRING_TOKEN = get("FLEET_PAIRING_TOKEN")
FLEET_WORKER_ID = get("FLEET_WORKER_ID")
FLEET_WORKER_NAME = get("FLEET_WORKER_NAME")
FLEET_MAX_JOBS = max(1, get_int("FLEET_MAX_JOBS", 1))
FLEET_HEARTBEAT_SECONDS = max(2, get_int("FLEET_HEARTBEAT_SECONDS", 5))
FLEET_STALE_SECONDS = max(30, get_int("FLEET_STALE_SECONDS", 120))


# Script models the UI can pick from ("format" selects payload/parse shape).
# "cli" is special-cased in llm.chat(): no HTTP call at all, goes straight to
# the local `claude` CLI under this machine's own subscription — for when
# kie.ai's proxy itself is the thing that's unreliable, not just occasional
# hiccups a retry can heal.
SCRIPT_MODELS = {
    "sonnet-5": {"format": "anthropic", "base": LLM_BASE_URL,
                 "path": LLM_CHAT_PATH, "model": LLM_MODEL, "key": LLM_API_KEY},
    "claude-cli": {"format": "cli"},
    "gpt-5.6-terra": {"format": "responses", "base": KIE_BASE_URL,
                      "path": "/codex/v1/responses", "model": "gpt-5-6-terra",
                      "key": LLM_GPT_API_KEY},
    "gpt-5.6-luna": {"format": "responses", "base": KIE_BASE_URL,
                     "path": "/codex/v1/responses", "model": "gpt-5-6-luna",
                     "key": LLM_GPT_API_KEY},
    "gpt-5.6-sol": {"format": "responses", "base": KIE_BASE_URL,
                    "path": "/codex/v1/responses", "model": "gpt-5-6-sol",
                    "key": LLM_GPT_API_KEY},
    "grok-4-5": {"format": "responses", "base": KIE_BASE_URL,
                "path": "/grok/v1/responses", "model": "grok-4-5",
                "key": LLM_GROK_API_KEY},
    "openrouter-monitor": {
        "format": "openai", "base": OPENROUTER_BASE_URL,
        "path": "/chat/completions", "model": OPENROUTER_MONITOR_MODEL,
        "key": OPENROUTER_API_KEY, "reasoning": True,
        "reasoning_max_tokens": 256,
    },
}


def missing_keys() -> list[str]:
    required = {
        "LLM_API_KEY": LLM_API_KEY,
        "LLM_BASE_URL": LLM_BASE_URL,
        "KIE_API_KEY": KIE_API_KEY,
        "AI33_API_KEY": AI33_API_KEY or AI33_API_KEY_BACKUP,
        "PIXABAY_API_KEY": PIXABAY_API_KEY,
    }
    return [k for k, v in required.items() if not v]


def find_font() -> str:
    if CAPTION_FONT_FILE and Path(CAPTION_FONT_FILE).exists():
        return CAPTION_FONT_FILE
    candidates = [
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        # Windows (a render worker needs a real font or captions vanish)
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        # Linux
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
