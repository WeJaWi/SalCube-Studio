#!/usr/bin/env python3
"""Keep the main studio current and running under one small supervisor."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
OUTPUT = ROOT / "output"
STATE_FILE = OUTPUT / "update_status.json"
UPDATE_LOCK = OUTPUT / ".southcube-update.lock"
INTERVAL = max(60, int(os.environ.get("SOUTHCUBE_UPDATE_INTERVAL_SECONDS", "300")))
PUBLIC_URL = os.environ.get("SOUTHCUBE_PUBLIC_URL", "http://100.72.62.87:5066/")
ACTIVE = {"queued", "researching", "awaiting_title", "running", "awaiting_approval",
          "dispatched", "uploading", "retry_wait"}
child: subprocess.Popen | None = None
stopping = False
_SECRET = re.compile(r"(?:sk[-_][A-Za-z0-9_-]{12,}|Bearer\s+\S+|https?://[^\s/@]+:[^\s/@]+@)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_state(state: str, message: str, **fields) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        pass
    safe_message = _SECRET.sub("[redacted]", str(message))[:500]
    data = {**current, "state": state, "message": safe_message,
            "supervised": True, "auto_update": True, "interval_seconds": INTERVAL,
            "url": PUBLIC_URL, **fields}
    fd, name = tempfile.mkstemp(prefix=".southcube-update-", dir=OUTPUT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
        os.replace(name, STATE_FILE)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def git(*args: str, timeout: int = 120, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise RuntimeError(detail[-500:])
    return result.stdout.strip()


def revision(ref: str) -> str:
    return git("rev-parse", "--short=12", ref)


def inspect_remote() -> dict:
    checked_at = utc_now()
    write_state("checking", "Checking GitHub for the newest main revision",
                last_checked_at=checked_at)
    git("fetch", "--quiet", "origin", "main")
    local = revision("HEAD")
    remote = revision("origin/main")
    if local == remote:
        write_state("current", "Newest GitHub version is running",
                    local_revision=local, remote_revision=remote,
                    last_checked_at=checked_at)
        return {"state": "current", "local": local, "remote": remote}
    remote_is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
        cwd=ROOT, timeout=30, check=False).returncode == 0
    if remote_is_ancestor:
        write_state("local_ahead", "Local main includes the newest GitHub revision",
                    local_revision=local, remote_revision=remote,
                    last_checked_at=checked_at)
        return {"state": "local_ahead", "local": local, "remote": remote}
    branch = git("branch", "--show-current")
    if branch != "main":
        message = f"Automatic update blocked: checked-out branch is {branch or 'detached'}"
        write_state("blocked", message, local_revision=local,
                    remote_revision=remote, last_checked_at=checked_at)
        return {"state": "blocked", "local": local, "remote": remote}
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        write_state("blocked", "Automatic update blocked by tracked local changes",
                    local_revision=local, remote_revision=remote,
                    last_checked_at=checked_at)
        return {"state": "blocked", "local": local, "remote": remote}
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
                              cwd=ROOT, timeout=30, check=False).returncode == 0
    if not ancestor:
        write_state("blocked", "Automatic update blocked because main and origin/main diverged",
                    local_revision=local, remote_revision=remote,
                    last_checked_at=checked_at)
        return {"state": "blocked", "local": local, "remote": remote}
    return {"state": "available", "local": local, "remote": remote,
            "last_checked_at": checked_at}


def queue_busy() -> tuple[bool, int]:
    try:
        jobs = json.loads((OUTPUT / "queue_state.json").read_text()).get("jobs") or []
    except (OSError, ValueError, AttributeError):
        return True, 0
    count = sum(isinstance(job, dict) and job.get("status") in ACTIVE for job in jobs)
    return count > 0, count


def digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def apply_update(info: dict) -> bool:
    before_requirements = digest(ROOT / "requirements.txt")
    write_state("applying", "Applying verified fast-forward update",
                local_revision=info["local"], remote_revision=info["remote"],
                last_checked_at=info.get("last_checked_at"))
    git("merge", "--ff-only", "origin/main")
    after_requirements = digest(ROOT / "requirements.txt")
    if before_requirements != after_requirements:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                                 str(ROOT / "requirements.txt")], cwd=ROOT,
                                timeout=900, check=False)
        if result.returncode:
            raise RuntimeError("dependency installation failed")
    current = revision("HEAD")
    write_state("restarting", "Update applied; restarting SalCube Studio",
                local_revision=current, remote_revision=info["remote"],
                last_checked_at=info.get("last_checked_at"), last_updated_at=utc_now())
    return True


def stop_child() -> None:
    global child
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def handle_stop(_signum, _frame) -> None:
    global stopping
    stopping = True
    stop_child()


def start_app() -> subprocess.Popen:
    env = dict(os.environ)
    env.update(SOUTHCUBE_SUPERVISED="1", PYTHONUNBUFFERED="1")
    UPDATE_LOCK.unlink(missing_ok=True)
    process = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=ROOT, env=env)
    write_state("current", "SalCube Studio is running and automatic updates are active",
                local_revision=revision("HEAD"), pid=process.pid, started_at=utc_now())
    return process


def main() -> int:
    global child
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    UPDATE_LOCK.unlink(missing_ok=True)
    try:
        initial = inspect_remote()
        if initial["state"] == "available" and apply_update(initial):
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
    except Exception as exc:  # noqa: BLE001
        write_state("error", f"Update check failed: {type(exc).__name__}: {exc}")
    while not stopping:
        child = start_app()
        next_check = time.monotonic() + INTERVAL
        while not stopping and child.poll() is None:
            time.sleep(1)
            if time.monotonic() < next_check:
                continue
            next_check = time.monotonic() + INTERVAL
            try:
                info = inspect_remote()
                if info["state"] != "available":
                    continue
                UPDATE_LOCK.touch(exist_ok=True)
                time.sleep(1)
                busy, count = queue_busy()
                if busy:
                    UPDATE_LOCK.unlink(missing_ok=True)
                    write_state("waiting_for_idle",
                                f"GitHub update ready; waiting for {count or 'unknown'} active or queued job(s)",
                                local_revision=info["local"], remote_revision=info["remote"],
                                last_checked_at=info.get("last_checked_at"))
                    continue
                stop_child()
                if apply_update(info):
                    UPDATE_LOCK.unlink(missing_ok=True)
                    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
            except Exception as exc:  # noqa: BLE001
                UPDATE_LOCK.unlink(missing_ok=True)
                write_state("error", f"Automatic update failed: {type(exc).__name__}: {exc}")
        if not stopping:
            code = child.returncode if child else None
            write_state("restarting", f"Studio process exited ({code}); restarting", last_exit_code=code)
            time.sleep(3)
    UPDATE_LOCK.unlink(missing_ok=True)
    write_state("stopped", "SouthCube supervisor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
