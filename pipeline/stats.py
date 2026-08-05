"""Per-profile render-time and cost statistics for the queue's size/ETA/cost
display.

Backs output/render_stats.json, with the same atomic-write treatment as
config.py's settings.json. Records only the RENDER phase (grant -> finish),
deliberately excluding queue wait and both approval/render gates, so a
sample reflects actual CPU-bound render throughput rather than how long a
job happened to sit idle.
"""
from __future__ import annotations

import json
import statistics
import threading
from typing import List, Optional

from . import config

STATS_FILE = config.OUTPUT_DIR / "render_stats.json"
_STATS_LOCK = threading.Lock()

MAX_SAMPLES_PER_PROFILE = 20
MIN_SAMPLES_TO_TRUST = 3
# Plausible starting point (not measured) — real per-profile data supersedes
# this within a handful of completed jobs. A backfill from historical
# script.txt/final.mp4 mtimes was tried and rejected: those timestamps
# include unrelated queueing/idle time (a 6x spread across otherwise
# similar-length jobs), so seeding from bad data would be worse than
# seeding from a labeled guess.
SEED_CHARS_PER_MINUTE = 600.0


def _load() -> dict:
    try:
        raw = json.loads(STATS_FILE.read_text())
    except Exception:  # noqa: BLE001
        raw = {}
    if not isinstance(raw, dict) or "samples" not in raw:
        raw = {"version": 1, "samples": {}}
    return raw


def _save(data: dict) -> None:
    tmp = STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(STATS_FILE)


def record_render(profile: str, job_id: str, chars: int, beats: int,
                  render_seconds: float, gpu: bool, finished_at: str) -> None:
    """Append one completed render's timing, capped at MAX_SAMPLES_PER_PROFILE
    (oldest dropped first) so the average tracks recent hardware/settings
    rather than a job from months ago."""
    if render_seconds <= 0 or chars <= 0:
        return  # a hard-crash / instant-resume timing isn't a real sample
    with _STATS_LOCK:
        data = _load()
        samples = data["samples"].setdefault(profile, [])
        samples.append({
            "job_id": job_id, "chars": chars, "beats": beats,
            "render_seconds": render_seconds, "gpu": gpu,
            "finished_at": finished_at,
        })
        data["samples"][profile] = samples[-MAX_SAMPLES_PER_PROFILE:]
        _save(data)


def _chars_per_minute(samples: List[dict]) -> Optional[float]:
    if len(samples) < MIN_SAMPLES_TO_TRUST:
        return None
    rates = [s["chars"] / (s["render_seconds"] / 60.0)
             for s in samples if s.get("render_seconds")]
    return statistics.median(rates) if rates else None


def chars_per_render_minute(profile: str) -> float:
    """Median chars/render-minute for `profile`'s own history; falls back to
    the pooled median across all profiles once that has enough samples,
    then to the seed constant. Kept per-profile because host-enabled
    profiles (HeyGen avatar work) render slower per character than a
    no-host classic-style profile — one global number would misestimate
    both directions."""
    data = _load()
    own = _chars_per_minute(data["samples"].get(profile, []))
    if own:
        return own
    pooled = [s for samples in data["samples"].values() for s in samples]
    all_rate = _chars_per_minute(pooled)
    return all_rate or SEED_CHARS_PER_MINUTE


def eta_seconds(profile: str, chars: int) -> int:
    """Estimated render-phase duration in seconds. Clamped to [0.25x, 4x] of
    the seed-based estimate so one crash-inflated or freakishly-fast sample
    can't produce an absurd ETA for every job queued after it."""
    rate = chars_per_render_minute(profile)
    seed_minutes = chars / SEED_CHARS_PER_MINUTE
    minutes = chars / rate if rate > 0 else seed_minutes
    minutes = max(seed_minutes * 0.25, min(seed_minutes * 4, minutes))
    return max(1, int(minutes * 60))


# ── Cost estimate (display-only) ────────────────────────────────────────
# Placeholder per-unit costs — NOT calibrated against real invoices. Edit
# these once you have a few invoices to compare against; until then this
# is meant to answer "does this look about right before I queue a Big job
# overnight", not to be trusted to the cent.
COST_TABLE = {
    "llm_per_1k_chars": 0.03,    # outline+hooks+script+beat-plan (sonnet-5 via kie.ai)
    "tts_per_1k_chars": 0.05,    # ai33.pro voice synthesis
    "heygen_per_second": 0.02,   # host avatar clips — only charged if the profile's host is enabled
    "broll_per_beat": 0.01,      # kie.ai stock/AI b-roll + image average
    "music_flat": 0.05,          # ai33 Suno generation, per job
}


def estimate_cost_usd(chars: int, beats: int, host_enabled: bool,
                      host_seconds: float = 90.0) -> float:
    """Rough per-job cost estimate — a display-only approximation, not a
    real accounting figure (see COST_TABLE's own caveat)."""
    cost = (chars / 1000.0) * (COST_TABLE["llm_per_1k_chars"]
                               + COST_TABLE["tts_per_1k_chars"])
    cost += beats * COST_TABLE["broll_per_beat"]
    cost += COST_TABLE["music_flat"]
    if host_enabled:
        cost += host_seconds * COST_TABLE["heygen_per_second"]
    return round(cost, 2)
