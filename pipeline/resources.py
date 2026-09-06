"""One cancellation-aware FFmpeg budget for every job on this machine."""
from __future__ import annotations

import shutil
import threading
import time
import sys
import subprocess
import re
from contextlib import contextmanager

from . import cancel, config, telemetry

_condition = threading.Condition()
_active = 0
_waiting = 0
_disk_reserved = 0
_memory = (0.0, None)


def available_memory_gb():
    global _memory
    if time.monotonic() - _memory[0] < 2:
        return _memory[1]
    value = None
    try:
        if sys.platform == "win32":
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended")]
            info = Memory()
            info.length = ctypes.sizeof(info)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
                value = info.available / 1024**3
        elif sys.platform == "darwin":
            output = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
            page = int(re.search(r"page size of (\d+) bytes", output).group(1))
            count = sum(int(n) for n in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", output))
            value = page * count / 1024**3
        else:
            from pathlib import Path
            output = Path("/proc/meminfo").read_text()
            value = int(re.search(r"MemAvailable:\s+(\d+)", output).group(1)) / 1024**2
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        pass
    _memory = (time.monotonic(), value)
    return value


def limit():
    ram = max(1, int((config._detect_ram_gb() - 4) // 3))
    default = min(ram, 2 if config.gpu_encoder() else max(1, config._detect_cpu_count() // 3))
    try:
        return max(1, min(ram, 8, int(config.get("MAX_ACTIVE_ENCODERS", str(default)))))
    except ValueError:
        return max(1, default)


def snapshot():
    maximum = limit()
    available = available_memory_gb()
    with _condition:
        return {"active_encoders": _active, "waiting_encoders": _waiting,
                "max_encoders": maximum, "reserved_disk_bytes": _disk_reserved,
                "available_memory_gb": round(available, 2) if available is not None else None}


@contextmanager
def encode_slot():
    global _active, _waiting
    maximum = limit()
    with telemetry.span("encoder_wait"):
        with _condition:
            _waiting += 1
            try:
                while True:
                    cancel.check()
                    available = available_memory_gb()
                    if _active < maximum and (available is None or available >= 1.0):
                        break
                    if not _active and available is not None and available < 0.5:
                        raise RuntimeError("Less than 512 MB memory available; close other applications before rendering")
                    _condition.wait(0.2)
                cancel.check()
                _active += 1
            finally:
                _waiting -= 1
    try:
        yield
    finally:
        with _condition:
            _active -= 1
            _condition.notify_all()


@contextmanager
def reserve_disk(path, amount):
    global _disk_reserved
    amount = max(0, int(amount))
    with _condition:
        if shutil.disk_usage(path).free < _disk_reserved + amount + 1_000_000_000:
            raise RuntimeError("Insufficient disk space for concurrent final exports; retry after delivery frees space")
        _disk_reserved += amount
    try:
        yield
    finally:
        with _condition:
            _disk_reserved -= amount
