"""Lightweight, secret-free health telemetry for every SouthCube node."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - deliberately supported fallback
    psutil = None


_SECRET_FIELD = re.compile(
    r"(^|_)(api_?key|access_?key|secret|token|password|authorization|cookie|credential)s?($|_)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:sk[-_][A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~-]{12,}|"
    r"(?:access|refresh)_token=[^&\s]{8,})",
    re.IGNORECASE,
)
_SOURCE_SUFFIXES = {".py", ".html", ".js", ".css", ".json"}
_OUTPUT_NAMES = {"queue_state.json", "settings.json", "performance.json",
                 "manifest.json", "delivery_receipt.json", "inputs.json"}


@lru_cache(maxsize=1)
def _known_secret_values() -> tuple[str, ...]:
    values = []
    for name in dir(config):
        if not re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", name):
            continue
        value = getattr(config, name, None)
        if isinstance(value, str) and len(value) >= 8:
            values.append(value)
    return tuple(values)


def _clean_string(value: str) -> str:
    text = value[:2000]
    for secret in _known_secret_values():
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return _SECRET_VALUE.sub("[redacted]", text)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Return bounded JSON data with credential-shaped fields removed."""
    if _depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return _clean_string(value)
    if isinstance(value, dict):
        out = {}
        for raw_key, child in list(value.items())[:250]:
            key = _clean_string(str(raw_key))[:120]
            out[key] = "[redacted]" if _SECRET_FIELD.search(key) else sanitize(child, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize(child, _depth=_depth + 1) for child in list(value)[:250]]
    return _clean_string(str(value))


def _gb(value: int | float) -> float:
    return round(float(value) / 1_000_000_000, 2)


class NodeMonitor:
    """Collect host metrics in a background thread; snapshot never blocks."""

    def __init__(self, root: Path, output_dir: Path, interval: float = 15.0):
        self.root = Path(root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.interval = max(5.0, float(interval))
        self.instance_id = uuid.uuid4().hex[:16]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sample: dict = {
            "sampled_at": None, "instance_id": self.instance_id,
            "state": "sampling", "cpu": {}, "memory": {},
            "gpu": {"available": False, "devices": [], "note": "awaiting first sample"},
            "processes": [], "changes": [],
        }
        self._file_state: dict[str, tuple[int, str]] | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="southcube-node-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._sample))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._collect()
            except Exception as exc:  # noqa: BLE001
                sample = {"sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "instance_id": self.instance_id, "state": "degraded",
                          "cpu": {}, "memory": {}, "gpu": {"available": False,
                          "devices": [], "note": f"telemetry error: {type(exc).__name__}"},
                          "processes": [], "changes": []}
            with self._lock:
                self._sample = sanitize(sample)
            self._stop.wait(self.interval)

    def _collect(self) -> dict:
        cpu, memory, processes = self._system_metrics()
        return {
            "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instance_id": self.instance_id, "state": "ready",
            "cpu": cpu, "memory": memory, "gpu": self._gpu_metrics(),
            "processes": processes, "changes": self._changes(),
        }

    def _system_metrics(self) -> tuple[dict, dict, list[dict]]:
        cores = os.cpu_count() or 1
        try:
            load = round(os.getloadavg()[0], 2)
        except (AttributeError, OSError):
            load = None
        if psutil is None:
            return ({"percent": None, "cores": cores, "load_1m": load}, {}, [
                {"pid": os.getpid(), "name": "python", "rss_mb": None, "status": "running"}
            ])
        vm = psutil.virtual_memory()
        cpu = {"percent": round(psutil.cpu_percent(interval=None), 1),
               "cores": psutil.cpu_count(logical=True) or cores, "load_1m": load}
        memory = {"total_gb": _gb(vm.total), "available_gb": _gb(vm.available),
                  "used_percent": round(vm.percent, 1)}
        found = []
        try:
            parent = psutil.Process(os.getpid())
            targets = [parent, *parent.children(recursive=True)]
            for proc in targets[:80]:
                try:
                    info = proc.as_dict(attrs=["pid", "name", "status", "memory_info"])
                    found.append({"pid": info["pid"], "name": str(info.get("name") or "process")[:100],
                                  "rss_mb": round(info["memory_info"].rss / 1_000_000, 1),
                                  "status": str(info.get("status") or "unknown")[:40]})
                except (psutil.Error, OSError):
                    continue
        except (psutil.Error, OSError):
            pass
        return cpu, memory, found

    def _gpu_metrics(self) -> dict:
        command = shutil.which("nvidia-smi")
        if not command:
            return {"available": False, "devices": [],
                    "note": "NVIDIA telemetry unavailable; encoder capability is reported separately"}
        try:
            result = subprocess.run([
                command, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits"], capture_output=True, text=True,
                timeout=2, check=False)
            if result.returncode:
                raise OSError("nvidia-smi returned an error")
            devices = []
            for line in result.stdout.splitlines()[:8]:
                parts = [part.strip() for part in line.split(",")]
                if len(parts) != 4:
                    continue
                devices.append({"name": parts[0][:120],
                                "utilization_percent": float(parts[1]),
                                "memory_used_mb": float(parts[2]),
                                "memory_total_mb": float(parts[3])})
            return {"available": bool(devices), "devices": devices,
                    "note": "nvidia-smi" if devices else "No NVIDIA devices reported"}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {"available": False, "devices": [], "note": "GPU telemetry probe failed"}

    def _watched_files(self) -> dict[str, tuple[int, str]]:
        paths = [self.root / "app.py"]
        for folder in ("pipeline", "templates", "static"):
            base = self.root / folder
            if base.exists():
                paths.extend(path for path in base.rglob("*")
                             if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES)
        if self.output_dir.exists():
            for job_dir in list(self.output_dir.iterdir())[:1000]:
                if not job_dir.is_dir() or not job_dir.name.isdigit():
                    continue
                paths.extend(job_dir / name for name in _OUTPUT_NAMES
                             if (job_dir / name).is_file())
            paths.extend(self.output_dir / name for name in _OUTPUT_NAMES
                         if (self.output_dir / name).is_file())
        result = {}
        for path in sorted(paths, key=lambda p: str(p))[:1200]:
            try:
                stat = path.stat()
                if path.is_relative_to(self.output_dir):
                    label = "output/" + str(path.relative_to(self.output_dir))
                else:
                    label = str(path.relative_to(self.root))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[label] = (stat.st_size, digest)
            except (OSError, ValueError):
                continue
        return result

    def _changes(self) -> list[dict]:
        current = self._watched_files()
        previous, self._file_state = self._file_state, current
        if previous is None:
            return []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changes = []
        for path in sorted(set(previous) | set(current)):
            if previous.get(path) == current.get(path):
                continue
            kind = "created" if path not in previous else "deleted" if path not in current else "modified"
            changes.append({"id": f"{self.instance_id}:{time.time_ns()}:{len(changes)}",
                            "at": now, "kind": kind, "path": path,
                            "message": f"{path} {kind}"})
        return changes[-40:]
