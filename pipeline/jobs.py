"""Qt-free job manager for Health Studio.

Adapted from Wexilo's core/jobs.py + workflow_runner.py.

Provides:
- JSONL logging per job
- Manual jobs (for orchestrator pipeline steps)
- Background FFmpeg/process jobs with progress parsing
- Simple in-memory registry + callbacks
- Cancel support

Usage:
    from . import jobs
    jm = jobs.JobManager.instance()

    job_id = jm.start_manual_job(meta={"tool": "render", "title": "My Video"}, tag="render")
    jm.manual_log(job_id, "starting...")
    jm.manual_progress(job_id, 42)
    jm.manual_finish(job_id, 0)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config

logger = __import__("logging").getLogger(__name__)


def _logs_root() -> Path:
    p = config.ROOT / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_dir(p: Path | str) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _which(exe: str) -> Optional[str]:
    return shutil.which(exe)


def _ff_version() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    ff = _which("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    fp = _which("ffprobe.exe" if os.name == "nt" else "ffprobe")
    for tag, path in (("ffmpeg", ff), ("ffprobe", fp)):
        if path:
            try:
                out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=3)
                info[tag] = {"path": path, "version_line": out.stdout.splitlines()[0] if out.stdout else ""}
            except Exception:
                info[tag] = {"path": path, "version_line": ""}
    info["python"] = __import__("platform").python_version()
    info["platform"] = {"system": os.name, "machine": __import__("platform").machine()}
    return info


class JSONLogWriter:
    def __init__(self, folder: Path | str, tag: str):
        folder = _ensure_dir(folder)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = folder / f"{tag}_{ts}_{uuid.uuid4().hex[:8]}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        logger.debug("Created JSON log file %s", self.path)

    def append(self, event: Dict[str, Any]):
        event["ts"] = _now_iso()
        self._fh.write(_safe_json_dump(event) + "\n")

    def close(self):
        try:
            self._fh.close()
            logger.debug("Closed JSON log file %s", self.path)
        except Exception:
            logger.exception("Failed to close log file %s", self.path)


class ManualJob:
    """Non-process job (e.g. the full video pipeline)."""

    def __init__(self, job_id: str, meta: Dict[str, Any], log_writer: JSONLogWriter, cancel_cb: Optional[Callable] = None):
        self.id = job_id
        self.meta = meta or {}
        self.log = log_writer
        self._last_pct = 0
        self._cancel_cb = cancel_cb
        self._cancelled = False

    def start(self):
        self.log.append({"kind": "start", "job_id": self.id, "meta": self.meta, "env": _ff_version()})
        logger.info("Manual job %s starting", self.id)

    def set_progress(self, pct: int):
        pct = min(100, max(0, int(pct)))
        if pct != self._last_pct:
            self._last_pct = pct
            self.log.append({"kind": "progress_kv", "job_id": self.id, "k": "pct", "v": str(pct)})
            logger.info("Manual job %s progress %s%%", self.id, pct)

    def write_log(self, line: str):
        s = str(line or "")
        self.log.append({"kind": "stdout", "job_id": self.id, "line": s})
        logger.debug("Manual job %s log: %s", self.id, s)

    def finish(self, exit_code: int = 0):
        self.log.append({"kind": "finish", "job_id": self.id, "exit_code": int(exit_code)})
        code = int(exit_code)
        if code == 0:
            logger.info("Manual job %s finished successfully", self.id)
        else:
            logger.warning("Manual job %s finished with exit code %s", self.id, code)
        self.log.close()

    def cancel(self):
        self._cancelled = True
        self.log.append({"kind": "cancel", "job_id": self.id})
        logger.info("Cancel requested for manual job %s", self.id)
        if self._cancel_cb:
            try:
                self._cancel_cb()
            except Exception:
                logger.exception("Manual job %s cancel callback failed", self.id)


class BackgroundProcessJob:
    """Runs a subprocess (typically ffmpeg with -progress pipe:1) and parses progress."""

    def __init__(self, job_id: str, cmd: list, duration_s: float, meta: Dict[str, Any], log_writer: JSONLogWriter, cancel_cb: Optional[Callable] = None):
        self.id = job_id
        self.cmd = cmd
        self.duration_ms = max(1.0, duration_s) * 1000.0
        self.meta = meta or {}
        self.log = log_writer
        self._cancel_cb = cancel_cb
        self._proc: Optional[subprocess.Popen] = None
        self._last_pct = 0
        self._cancelled = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self.log.append({"kind": "start", "job_id": self.id, "cmd": self.cmd, "meta": self.meta, "env": _ff_version()})
        logger.info("Background job %s starting", self.id)
        logger.debug("Command: %s", " ".join(map(str, self.cmd)))

        # stderr merged into stdout: reading the two pipes sequentially can
        # deadlock once one buffer fills (ffmpeg logs heavily to stderr).
        self._proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self):
        assert self._proc is not None
        try:
            for line in self._proc.stdout or []:
                if self._cancelled:
                    break
                self._parse_progress(line.strip())
        except Exception as e:
            self.log.append({"kind": "error", "job_id": self.id, "error": str(e)})
            logger.exception("Job %s monitor error", self.id)
        finally:
            if self._proc:
                self._proc.wait()
                exit_code = self._proc.returncode or 0
                self.log.append({"kind": "finish", "job_id": self.id, "exit_code": int(exit_code)})
                if exit_code == 0:
                    logger.info("Job %s finished successfully", self.id)
                else:
                    logger.warning("Job %s finished with exit code %s", self.id, exit_code)
            self.log.close()

    def _parse_progress(self, line: str):
        if not line:
            return
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            self.log.append({"kind": "progress_kv", "job_id": self.id, "k": k, "v": v})
            if k == "out_time_ms" and v.isdigit():
                try:
                    ms = float(v)
                    pct = min(99, int(5 + (ms / self.duration_ms) * 93))
                    if pct != self._last_pct:
                        self._last_pct = pct
                except Exception:
                    pass
            elif k in ("pct", "progress_pct"):
                try:
                    pct = min(100, max(0, int(float(v))))
                    if pct != self._last_pct:
                        self._last_pct = pct
                except Exception:
                    pass
        else:
            self.log.append({"kind": "stdout", "job_id": self.id, "line": line})

    def cancel(self, wait_s: float = 2.0):
        self._cancelled = True
        self.log.append({"kind": "cancel", "job_id": self.id})
        if self._proc:
            try:
                self._proc.terminate()
                time.sleep(0.5)
                if self._proc.poll() is None:
                    self._proc.kill()
            except Exception:
                pass
        if self._cancel_cb:
            try:
                self._cancel_cb()
            except Exception:
                pass


class JobManager:
    """Singleton job manager (thread-safe enough for this use case)."""

    _instance: Optional["JobManager"] = None
    _lock = threading.Lock()

    def __init__(self):
        self.jobs: Dict[str, Any] = {}
        self._root = _logs_root()
        logger.info("JobManager initialized (logs at %s)", self._root)

    @classmethod
    def instance(cls) -> "JobManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _make_writer(self, tag: str) -> JSONLogWriter:
        folder = _ensure_dir(self._root / tag)
        return JSONLogWriter(folder, tag)

    # --- Public API ---

    def start_manual_job(self, meta: Dict[str, Any], tag: str = "general", cancel_cb: Optional[Callable] = None) -> str:
        job_id = uuid.uuid4().hex
        writer = self._make_writer(tag)
        meta = dict(meta or {})
        meta["log_path"] = str(writer.path)
        j = ManualJob(job_id, meta, writer, cancel_cb=cancel_cb)
        self.jobs[job_id] = j
        j.start()
        logger.info("Started manual job %s (tag=%s)", job_id, tag)
        return job_id

    def manual_progress(self, job_id: str, pct: int):
        j = self.jobs.get(job_id)
        if isinstance(j, ManualJob):
            j.set_progress(pct)

    def manual_log(self, job_id: str, line: str):
        j = self.jobs.get(job_id)
        if isinstance(j, ManualJob):
            j.write_log(line)

    def manual_finish(self, job_id: str, exit_code: int = 0):
        j = self.jobs.get(job_id)
        if isinstance(j, ManualJob):
            j.finish(exit_code)
            self.jobs.pop(job_id, None)

    def start_ffmpeg_job(self, cmd: list, duration_s: float, meta: Dict[str, Any], tag: str = "render") -> str:
        job_id = uuid.uuid4().hex
        writer = self._make_writer(tag)
        meta = dict(meta or {})
        meta["log_path"] = str(writer.path)
        j = BackgroundProcessJob(job_id, cmd, duration_s, meta, writer)
        self.jobs[job_id] = j
        j.start()
        logger.info("Started ffmpeg job %s (tag=%s)", job_id, tag)
        return job_id

    def start_process_job(self, cmd: list, meta: Dict[str, Any], tag: str = "general", duration_s: float = 60.0) -> str:
        return self.start_ffmpeg_job(cmd, duration_s, meta, tag)

    def cancel(self, job_id: str):
        j = self.jobs.get(job_id)
        if j:
            try:
                if hasattr(j, "cancel"):
                    j.cancel()
            except Exception:
                logger.exception("Failed to cancel job %s", job_id)

    def get_log_path(self, job_id: str) -> Optional[str]:
        j = self.jobs.get(job_id)
        if j and hasattr(j, "log"):
            return str(j.log.path)
        return None

    def shutdown(self):
        for job_id in list(self.jobs.keys()):
            self.cancel(job_id)
        self.jobs.clear()


# --- Workflow runner support (simplified, no Qt) ---

class WorkflowCancelled(RuntimeError):
    pass


def run_cancellable(cmd: list, ctx: Dict[str, Any], poll_s: float = 0.2) -> None:
    """Run subprocess, respect ctx['should_stop']().

    Output is spooled to a temp file, not PIPE: an undrained pipe fills up
    and deadlocks chatty commands (ffmpeg) while we poll.
    """
    import tempfile
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf,
                                text=True)
        try:
            while proc.poll() is None:
                if ctx.get("should_stop", lambda: False)():
                    proc.kill()
                    proc.wait(5)
                    raise WorkflowCancelled("cancelled")
                time.sleep(poll_s)
            if proc.returncode != 0:
                errf.seek(0)
                err = errf.read() or ""
                tail = "\n".join(err.strip().splitlines()[-6:])
                raise RuntimeError(tail or f"command failed with exit code {proc.returncode}")
        finally:
            if proc.poll() is None:
                proc.kill()
