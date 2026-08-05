"""Pure queue-scheduling logic — no Flask, no threads, no file I/O, no
reference to app.py's JOBS dict. Kept pure so it's safe to call while
app.py holds its LOCK (preserving the existing claim-under-lock invariant)
and so it's testable in isolation.

This module grows incrementally alongside the queue/scheduling feature:
today it only classifies a job's size for display; job ordering and
dispatch land here once the render-gate/two-phase-queue work is in.
"""
from __future__ import annotations

from datetime import datetime, time as dt_time


def classify_size(chars: int, small_threshold: int, big_threshold: int) -> str:
    """"small" | "medium" | "big", per the settings-configured thresholds."""
    if chars >= big_threshold:
        return "big"
    if chars <= small_threshold:
        return "small"
    return "medium"


def is_night(now: datetime, start: str, end: str) -> bool:
    """Whether `now`'s local time falls inside the configured night window,
    e.g. is_night(now, "22:00", "07:00") — handles a window that wraps past
    midnight (start > end) as well as one that doesn't."""
    def _parse(s: str) -> dt_time:
        h, m = s.split(":")
        return dt_time(int(h), int(m))

    t = now.time()
    s, e = _parse(start), _parse(end)
    if s <= e:
        return s <= t < e
    return t >= s or t < e
