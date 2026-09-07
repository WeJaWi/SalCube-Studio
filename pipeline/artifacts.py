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


def file_digest(path: Path) -> str:
    """Bounded-memory content fingerprint; refuse files changing during a read."""
    before = signature(path)
    def read_hash():
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                checksum.update(chunk)
        return checksum.hexdigest()
    checksum = read_hash()
    after = signature(path)
    if before is None or after is None or before[:3] != after[:3]:
        raise OSError(f"File changed while checking its contents: {path.name}")
    if after != before:
        # Indexing may arrive just after the encoder's rename. Prove both
        # reads agree and the second read is stable before accepting it.
        if read_hash() != checksum or signature(path) != after:
            raise OSError(f"File changed while checking its contents: {path.name}")
    return checksum


def output_matches(record: dict, path: Path) -> bool:
    """Keep strict checks, but tolerate ctime-only changes proven by SHA-256.

    macOS metadata/indexing and hardlinks can alter ctime without touching
    video bytes. Older receipts lacking a digest still fail closed on drift.
    Intermediate cache contracts retain their existing strict signatures.
    """
    current = signature(path)
    saved = record.get("outputs")
    if current is None or not isinstance(saved, list) or len(saved) != 1:
        return False
    if saved == [current]:
        return True
    previous = saved[0]
    hashes = record.get("sha256") or {}
    expected = hashes.get(current[0]) if isinstance(hashes, dict) else None
    if not isinstance(previous, list) or previous[:3] != current[:3] or not expected:
        return False
    try:
        return file_digest(path) == expected
    except OSError:
        return False


def publish(path: Path, contract, outputs, *, hash_outputs: bool = False) -> None:
    outputs = list(outputs)
    signatures = [signature(p) for p in outputs]
    data = {"version": 1, "input": digest(contract), "outputs": signatures}
    if hash_outputs:
        data["sha256"] = {str(p.resolve()): file_digest(p) for p in outputs}
        current = [signature(p) for p in outputs]
        if any(old is None or new is None or old[:3] != new[:3] for old, new in zip(signatures, current)):
            raise OSError("Export changed while preparing its verification receipt")
        data["outputs"] = current
    write(path, data)
