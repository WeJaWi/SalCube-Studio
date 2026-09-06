"""Versioned, atomic contracts for derived media. No provider credentials."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def signature(path):
    if not path:
        return None
    path = Path(path)
    try:
        s = path.stat()
        return [str(path.resolve()), s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    except OSError:
        return None


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def matches(path: Path, contract, outputs) -> bool:
    data = read(path)
    return (data.get("version") == 1 and data.get("input") == digest(contract)
            and data.get("outputs") == [signature(p) for p in outputs]
            and all(signature(p) is not None for p in outputs))


def publish(path: Path, contract, outputs) -> None:
    write(path, {"version": 1, "input": digest(contract),
                 "outputs": [signature(p) for p in outputs]})
