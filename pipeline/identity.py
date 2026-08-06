"""Opaque public identifiers for videos, thumbnails, and reports."""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Iterable


ID_LENGTH = 20


def numeric_id(*, taken: Iterable[str] = (), directories: Iterable[Path] = ()) -> str:
    """Return a collision-checked, exactly 20-digit cryptographic ID."""
    used = set(taken)
    dirs = tuple(Path(d) for d in directories)
    for _ in range(100):
        value = "".join(secrets.choice("0123456789") for _ in range(ID_LENGTH))
        if value in used or any((d / value).exists() for d in dirs):
            continue
        return value
    raise RuntimeError("could not allocate a unique 20-digit media ID")


def is_numeric_id(value: str) -> bool:
    return len(value) == ID_LENGTH and value.isdigit()
