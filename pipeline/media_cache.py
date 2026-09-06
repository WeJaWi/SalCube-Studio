"""Bounded stock-search and media caches; never cache a selection decision.

Searches/downloads for one key are coalesced across job threads. Cache files
are atomically published and copied (not hard-linked) into each job, so job
cleanup or modification cannot damage shared originals. Separate processes
may fetch the same key, but unique temporary files keep publication safe.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import requests
from PIL import Image

from . import cancel, clients, config, telemetry

_SEARCHES: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_SEARCH_LOCK = threading.Lock()
_KEY_LOCKS = tuple(threading.Lock() for _ in range(128))
_PRUNE_LOCK = threading.Lock()
_CACHE_NAME = re.compile(r"^[a-f0-9]{64}\.(?:mp4|jpg)$")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _lock(key: str):
    return _KEY_LOCKS[int(key[:8], 16) % len(_KEY_LOCKS)]


@contextmanager
def _locked(key: str):
    lock = _lock(key)
    while not lock.acquire(timeout=0.2):
        cancel.check()
    try:
        cancel.check()
        yield
    finally:
        lock.release()


def search_json(url: str, *, params: dict, headers: dict | None = None) -> dict:
    """Cache successful provider results, including empty results, for 24h.

    Keys include a hash of credentials so switching accounts never reuses
    the other account's search. Exceptions are deliberately not cached.
    """
    params = dict(params)
    for field in ("q", "query"):
        if isinstance(params.get(field), str):
            params[field] = " ".join(params[field].split()).casefold()
    key = _digest([url, params, headers])
    ttl = max(0, config.STOCK_SEARCH_CACHE_TTL_SECONDS)
    if url.startswith("https://pixabay.com/api/"):
        ttl = max(86400, ttl)  # Pixabay's documented minimum cache period
    limit = max(1, config.STOCK_SEARCH_CACHE_MAX_ENTRIES)
    root = config.OUTPUT_DIR / ".stock-cache" / "search"
    saved = root / (key + ".json")
    with _locked(key):
        now = time.time()
        with _SEARCH_LOCK:
            for stale in [k for k, (expires, _) in _SEARCHES.items()
                          if expires <= now]:
                _SEARCHES.pop(stale, None)
            item = _SEARCHES.get(key)
            if ttl and limit and item is not None:
                telemetry.count("stock_search_cache_hits")
                _SEARCHES.move_to_end(key)
                return copy.deepcopy(item[1])
        if ttl:
            try:
                saved_data = json.loads(saved.read_text())
                if (saved_data["expires"] > now
                        and isinstance(saved_data["data"], dict)):
                    _remember_search(key, saved_data["expires"], saved_data["data"], limit)
                    telemetry.count("stock_search_cache_hits")
                    return saved_data["data"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        cancel.check()
        response = clients._retrying(lambda: requests.get(url, params=params, headers=headers, timeout=60))
        try:
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("stock provider returned a non-object response")
        finally:
            response.close()
        if ttl:
            expires = time.time() + ttl
            _remember_search(key, expires, data, limit)
            try:
                with _temporary(root, ".json") as tmp:
                    tmp.write_text(json.dumps({"expires": expires, "data": data}))
                    tmp.replace(saved)
                # Disk retention is bounded independently from the hot memory LRU.
                _prune_searches(root, limit, ttl)
            except OSError:
                pass  # an unavailable cache must not discard a valid search
        return data


def _remember_search(key: str, expires: float, data: dict, limit: int) -> None:
    with _SEARCH_LOCK:
        _SEARCHES[key] = (expires, copy.deepcopy(data))
        _SEARCHES.move_to_end(key)
        while len(_SEARCHES) > limit:
            _SEARCHES.popitem(last=False)


def _prune_searches(root: Path, limit: int, ttl: int) -> None:
    with _PRUNE_LOCK:
        entries = []
        for path in root.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name) or path.is_symlink():
                continue
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for index, (modified, path) in enumerate(sorted(entries, reverse=True)):
            if index >= limit or time.time() - modified >= max(86400, ttl):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def valid_media(path: Path, kind: str) -> bool:
    """Reject HTML error pages, incomplete images, and unusable MP4 streams."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if kind == "photo":
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                return image.width > 0 and image.height > 0
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        if probe.returncode or probe.stderr.strip():
            return False
        data = json.loads(probe.stdout)
        stream = (data.get("streams") or [{}])[0]
        duration = stream.get("duration")
        if not duration or duration == "N/A":
            duration = data.get("format", {}).get("duration")
        return (int(stream.get("width", 0)) > 0
                and int(stream.get("height", 0)) > 0
                and float(duration or 0) > 0)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


@contextmanager
def _temporary(parent: Path, suffix: str):
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".stock-", suffix=suffix, dir=parent)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _copy_atomic(source: Path, dest: Path) -> Path:
    with _temporary(dest.parent, dest.suffix) as tmp:
        shutil.copyfile(source, tmp)
        cancel.check()
        tmp.replace(dest)
    return dest


def _prune(root: Path) -> None:
    """Delete only our named entries, oldest first, within the configured cap."""
    cap = max(0, config.STOCK_CACHE_MAX_BYTES)
    ttl = max(0, config.STOCK_CACHE_TTL_SECONDS)
    with _PRUNE_LOCK:
        entries = []
        for path in root.iterdir():
            # A killed process cannot run its temporary-file cleanup. Only
            # reap our own old scratch files; never touch current downloads.
            if (path.name.startswith(".stock-") and not path.is_symlink()
                    and path.suffix in (".mp4", ".jpg")):
                try:
                    if time.time() - path.stat().st_mtime > 86400:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if not _CACHE_NAME.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                stat = path.stat()
                entries.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(size for _, size, _ in entries)
        for modified, size, path in sorted(entries):
            if total <= cap and time.time() - modified <= ttl:
                continue
            lock = _lock(path.stem)
            if not lock.acquire(blocking=False):
                continue  # an in-flight user will prune again when finished
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                pass
            finally:
                lock.release()


@telemetry.timed("stock_download")
def download(url: str, dest: Path, *, kind: str, cache_key: str,
             timeout: int = 180) -> Path:
    """Reuse a validated stock original or download it once into the cache."""
    dest = Path(dest)
    root = config.OUTPUT_DIR / ".stock-cache" / "media"
    key = _digest([kind, cache_key])
    suffix = ".mp4" if kind == "video" else ".jpg"
    cached = root / (key + suffix)
    cap = max(0, config.STOCK_CACHE_MAX_BYTES)
    ttl = max(0, config.STOCK_CACHE_TTL_SECONDS)
    if cap and ttl:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            cap = 0  # still download into the job when caching is unavailable
    with _locked(key):
        if cap and ttl and cached.is_file():
            try:
                fresh = time.time() - cached.stat().st_mtime <= ttl
                if fresh and valid_media(cached, kind):
                    telemetry.count("stock_media_cache_hits")
                    _copy_atomic(cached, dest)
                    try:
                        cached.touch()
                    except OSError:
                        pass
                    return dest
                cached.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # another process evicted it; fetch a fresh copy
        # Disabled caches still use a validated, atomic per-job download.
        with _temporary(root if cap and ttl else dest.parent, suffix) as tmp:
            clients.download(url, tmp, timeout=timeout)
            cancel.check()
            if not valid_media(tmp, kind):
                raise ValueError(f"stock download is not a valid {kind}")
            if cap and ttl and tmp.stat().st_size <= cap:
                tmp.replace(cached)
                _copy_atomic(cached, dest)
            else:
                _copy_atomic(tmp, dest)
    if cap and ttl:
        try:
            _prune(root)
        except OSError:
            pass  # cleanup must not turn a completed download into a failure
    return dest
