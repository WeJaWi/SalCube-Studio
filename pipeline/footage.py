"""Stock search across Pixabay AND Pexels — curated, no repeats.

Both providers are queried for every beat. Selection modes:
  "vision" — the top candidates' thumbnails are shown to the LLM next to the
             beat's narration; it picks the one whose actual picture matches
             (or none, triggering the fallback chain). Most accurate.
  "text"   — the LLM compares candidate metadata (tags/slug/alt) to the
             narration. No thumbnail downloads; near-free.
  "off"    — plain keyword overlap between the query and metadata (legacy).

`used` is a per-run set of provider-namespaced asset ids — footage never
repeats within one video.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from . import config

PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_API = "https://pixabay.com/api/"
PEXELS_VIDEO_API = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_API = "https://api.pexels.com/v1/search"

_STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "for", "to"}
_WORD = re.compile(r"[a-z]+")


@dataclass
class Hit:
    nid: str                            # provider-namespaced id (dedup key)
    score: float                        # 0..1 keyword overlap (pre-ranking)
    meta: str                           # tags / slug / alt text
    kind: str                           # "video" | "photo"
    duration: int                       # seconds (0 for photos)
    thumb: Optional[str]                # small preview URL for the vision judge
    download: Callable[[Path], Path]


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower())) - _STOP


def _score(query: str, meta_text: str) -> float:
    """0..1 — how much of the query the candidate's metadata covers."""
    q = _tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(meta_text)) / len(q)


def _download(url: str, dest: Path, timeout: int = 180) -> Path:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


# ───────────────────────── videos ───────────────────────────────
def _pixabay_videos(query: str, min_width: int) -> List[Hit]:
    if not config.PIXABAY_API_KEY:
        return []
    r = requests.get(PIXABAY_VIDEO_API, params={
        "key": config.PIXABAY_API_KEY, "q": query,
        "per_page": 25, "safesearch": "true"}, timeout=60)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    hits = [h for h in hits if h.get("duration", 0) >= 6] or hits
    out: List[Hit] = []
    for h in hits:
        def dl(dest: Path, h=h) -> Path:
            videos = h.get("videos", {})
            chosen = None
            for size in ("large", "medium", "small", "tiny"):
                v = videos.get(size)
                if v and v.get("url"):
                    chosen = v
                    if v.get("width", 0) >= min_width:
                        break
            if not chosen:
                raise RuntimeError("no downloadable variant")
            return _download(chosen["url"], dest)
        thumb = None
        for size in ("small", "tiny", "medium"):
            t = (h.get("videos", {}).get(size) or {}).get("thumbnail")
            if t:
                thumb = t
                break
        out.append(Hit(f"v:{h.get('id')}", _score(query, h.get("tags", "")),
                       h.get("tags", ""), "video",
                       int(h.get("duration", 0)), thumb, dl))
    return out


def _pexels_videos(query: str, min_width: int) -> List[Hit]:
    if not config.PEXELS_API_KEY:
        return []
    r = requests.get(PEXELS_VIDEO_API,
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": 25}, timeout=60)
    r.raise_for_status()
    vids = r.json().get("videos", [])
    vids = [v for v in vids if v.get("duration", 0) >= 6] or vids
    out: List[Hit] = []
    for v in vids:
        # relevance signal lives in the page URL slug, e.g.
        # pexels.com/video/a-woman-pouring-tea-853751/
        slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        meta = re.sub(r"\d+$", "", slug.replace("-", " ")).strip()
        def dl(dest: Path, v=v) -> Path:
            files = sorted(
                (f for f in v.get("video_files", [])
                 if (f.get("file_type") or "").endswith("mp4") and f.get("link")),
                key=lambda f: f.get("width") or 0)
            pick = next((f for f in files if (f.get("width") or 0) >= min_width),
                        files[-1] if files else None)
            if not pick:
                raise RuntimeError("no downloadable file")
            return _download(pick["link"], dest, timeout=300)
        thumb = v.get("image")
        if thumb:  # Pexels CDN resizes on the fly — keep the judge's copy small
            thumb = thumb.split("?")[0] + "?auto=compress&cs=tinysrgb&w=560"
        out.append(Hit(f"pxv:{v.get('id')}", _score(query, meta), meta,
                       "video", int(v.get("duration", 0)), thumb, dl))
    return out


# ───────────────────────── photos ───────────────────────────────
def _pixabay_photos(query: str) -> List[Hit]:
    if not config.PIXABAY_IMAGE_API_KEY:
        return []
    r = requests.get(PIXABAY_IMAGE_API, params={
        "key": config.PIXABAY_IMAGE_API_KEY, "q": query, "per_page": 25,
        "safesearch": "true", "orientation": "horizontal",
        "image_type": "photo"}, timeout=60)
    r.raise_for_status()
    out: List[Hit] = []
    for h in r.json().get("hits", []):
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        out.append(Hit(f"i:{h.get('id')}", _score(query, h.get("tags", "")),
                       h.get("tags", ""), "photo", 0,
                       h.get("webformatURL") or h.get("previewURL"),
                       lambda dest, url=url: _download(url, dest)))
    return out


def _pexels_photos(query: str) -> List[Hit]:
    if not config.PEXELS_API_KEY:
        return []
    r = requests.get(PEXELS_PHOTO_API,
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": 25,
                             "orientation": "landscape"}, timeout=60)
    r.raise_for_status()
    out: List[Hit] = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        url = src.get("large2x") or src.get("large")
        if not url:
            continue
        out.append(Hit(f"pxi:{p.get('id')}", _score(query, p.get("alt", "")),
                       p.get("alt", ""), "photo", 0, src.get("medium"),
                       lambda dest, url=url: _download(url, dest)))
    return out


# ───────────────────────── selection ────────────────────────────
def _gather(query: str, sources) -> List[Hit]:
    hits: List[Hit] = []
    for source in sources:
        try:
            hits += source(query)
        except Exception as e:  # noqa: BLE001
            print(f"[footage] {source.__name__} error for '{query}': {e}")
    return hits


def _select(hits: List[Hit], dest: Path, min_score: float,
            used: Optional[set], narration: str, query: str,
            smart: str) -> Optional[Path]:
    """Pick and download one hit. Smart modes ask the curator; if the judge
    is unavailable (network/LLM failure) we degrade to keyword scoring so a
    render never dies on the choosing step."""
    hits = [h for h in hits if not (used is not None and h.nid in used)]
    hits.sort(key=lambda h: -h.score)
    if smart in ("vision", "text") and hits:
        from . import curator
        pool = hits[:curator.MAX_CANDIDATES]
        for _ in range(3):  # a failed download removes the pick, re-judge
            pick = curator.choose(narration or query, query, pool, smart)
            if pick is None:
                break                       # judge unavailable → keyword path
            if pick < 0:
                return None                 # judge: nothing here fits
            h = pool[pick]
            try:
                path = h.download(dest)
            except Exception as e:  # noqa: BLE001
                print(f"[footage] download failed for {h.nid}: {e}")
                pool.remove(h)
                if not pool:
                    return None
                continue
            if used is not None:
                used.add(h.nid)
            return path
    for h in hits:
        if h.score < min_score:
            return None
        try:
            path = h.download(dest)
        except Exception as e:  # noqa: BLE001
            print(f"[footage] download failed for {h.nid}: {e}")
            continue
        if used is not None:
            used.add(h.nid)
        return path
    return None


def fetch_stock(query: str, dest: Path, min_width: int = 1280,
                min_score: float = 0.4, used: Optional[set] = None,
                narration: str = "", smart: str = "off") -> Optional[Path]:
    """Best-matching stock VIDEO across both providers, or None."""
    hits = _gather(query, (lambda q: _pixabay_videos(q, min_width),
                           lambda q: _pexels_videos(q, min_width)))
    return _select(hits, dest, min_score, used, narration, query, smart)


def fetch_stock_image(query: str, dest: Path, min_score: float = 0.4,
                      used: Optional[set] = None, narration: str = "",
                      smart: str = "off") -> Optional[Path]:
    """Best-matching stock PHOTO across both providers (Ken Burns at render)."""
    hits = _gather(query, (_pixabay_photos, _pexels_photos))
    return _select(hits, dest, min_score, used, narration, query, smart)
