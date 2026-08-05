"""Cooperative job cancellation.

Python cannot kill a thread, so a running job can only be stopped if it
agrees to stop. Two mechanisms together make that near-immediate rather
than "whenever the current stage happens to finish":

1. A per-job flag, checked at stage boundaries and between beats.
2. A registry of that job's live ffmpeg child processes, which ARE killable.
   A job spends nearly all its wall-clock inside ffmpeg, so killing the
   child is what makes Stop take effect in about a second instead of at the
   end of a 45-second beat.

The token is carried on a thread-local rather than threaded through the
~14 render._run() call sites. A job already owns its thread, so that works
directly — but the pipeline fans out into thread pools (beat compositing,
grouped TTS) whose worker threads do NOT inherit thread-locals, so those
few places must adopt the token explicitly. See adopt().
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable, Optional, Set


class JobCancelled(Exception):
    """Raised inside a job's own thread once it has been cancelled.

    Deliberately distinct from RuntimeError so callers can tell "the user
    stopped this" apart from "ffmpeg genuinely failed" — killing a child
    process otherwise surfaces as an ordinary `ffmpeg failed (signal 9)`.
    """


class CancelToken:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._cancelled = False
        self._procs: Set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> int:
        """Flag the job and kill any ffmpeg it currently has running.
        Returns how many processes were killed. Safe to call repeatedly and
        from any thread (the Flask request thread, typically)."""
        with self._lock:
            self._cancelled = True
            procs = list(self._procs)
        killed = 0
        for p in procs:
            try:
                if p.poll() is None:
                    p.kill()
                    killed += 1
            except Exception:  # noqa: BLE001
                pass  # already gone — nothing to do
        return killed

    def check(self) -> None:
        """Raise if cancelled. Call at points where stopping is clean."""
        if self.cancelled:
            raise JobCancelled(f"job {self.job_id} was stopped")

    def register(self, proc: subprocess.Popen) -> None:
        """Track a child process, killing it immediately if cancellation
        already happened — closes the race where cancel() runs between the
        caller's check() and its Popen()."""
        with self._lock:
            self._procs.add(proc)
            already = self._cancelled
        if already:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


_local = threading.local()


def set_current(token: Optional[CancelToken]) -> None:
    _local.token = token


def current() -> Optional[CancelToken]:
    return getattr(_local, "token", None)


def check() -> None:
    """Module-level convenience: no-op when the caller has no token, so
    pipeline code can call it unconditionally."""
    tok = current()
    if tok is not None:
        tok.check()


def sleep(seconds: float, step: float = 1.0) -> None:
    """time.sleep(), but wake promptly if the job gets cancelled.

    The pipeline waits a LOT: provider retry backoffs run up to 90s each,
    and the submit-then-poll loops wait up to 20 minutes for TTS/music/
    video. A plain sleep in any of those makes Stop look broken, because
    nothing checks the flag until the wait is over.
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def adopt(token: Optional[CancelToken], fn: Callable) -> Callable:
    """Wrap `fn` so it runs with `token` as its thread's current token.

    Needed at every ThreadPoolExecutor boundary: pool workers are separate
    threads and do not inherit the submitting thread's thread-local, so
    without this their ffmpeg children would be untracked and survive a
    Stop.
    """
    def _wrapped(*args, **kwargs):
        set_current(token)
        try:
            return fn(*args, **kwargs)
        finally:
            set_current(None)
    return _wrapped
