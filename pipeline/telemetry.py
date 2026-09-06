"""Per-attempt performance counters shared through the cancellation token."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from functools import wraps

from . import artifacts, cancel


class Metrics:
    def __init__(self, **context):
        self.context = context
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stages = {}
        self.counters = {}
        self.peak_rss_mb = 0.0

    def snapshot(self):
        with self.lock:
            return {**self.context, "elapsed_seconds": round(time.monotonic()-self.started, 3),
                    "stage_seconds_note": "Cumulative call durations; parallel and nested stages overlap.",
                    "stages": {k: dict(v) for k, v in self.stages.items()},
                    "counters": dict(self.counters), "peak_ffmpeg_rss_mb": round(self.peak_rss_mb, 1)}


def current():
    return getattr(cancel.current(), "metrics", None)


def count(name, amount=1):
    metrics = current()
    if metrics:
        with metrics.lock:
            metrics.counters[name] = metrics.counters.get(name, 0) + amount


def rss(value):
    metrics = current()
    if metrics:
        with metrics.lock:
            metrics.peak_rss_mb = max(metrics.peak_rss_mb, value)


@contextmanager
def span(name):
    metrics = current()
    start = time.monotonic()
    try:
        yield
    finally:
        if metrics:
            with metrics.lock:
                row = metrics.stages.setdefault(name, {"calls": 0, "seconds": 0})
                row["calls"] += 1
                row["seconds"] = round(row["seconds"] + time.monotonic()-start, 3)


def timed(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def save(path):
    metrics = current()
    if metrics:
        artifacts.write(path, metrics.snapshot())
