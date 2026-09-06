"""Stock search across Pixabay AND Pexels — curated, no repeats.

Both providers are queried for every beat. Selection modes:
  "vision" — the top candidates' thumbnails are shown to the LLM next to the
             beat's narration; it picks the one whose actual picture matches
             (or none, triggering the fallback chain). Most accurate.
  "text"   — the LLM compares candidate metadata (tags/slug/alt) to the
             narration. No thumbnail downloads; near-free.
  "off"    — plain keyword overlap between the query and metadata (legacy).

`used` is a per-run {provider-namespaced asset id: use count} map — each
clip may appear at most `max_repeats` times (default 1 — no repeats) before
it's excluded from selection. Raise it per profile if a long video needs to
lean on a small relevant pool instead of falling through to blank cards.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import artifacts, cancel, config, media_cache, telemetry

PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_API = "https://pixabay.com/api/"
PEXELS_VIDEO_API = "https://api.pexels.com/v1/videos/search"
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


def _download(url: str, dest: Path, timeout: int = 180, *,
              kind: str = "video", cache_key: str | None = None) -> Path:
    return media_cache.download(url, dest, kind=kind,
                                cache_key=cache_key or url, timeout=timeout)


# ───────────────────────── videos ───────────────────────────────
def _pixabay_videos(query: str, min_width: int) -> List[Hit]:
    if not config.PIXABAY_API_KEY:
        return []
    data = media_cache.search_json(PIXABAY_VIDEO_API, params={
        "key": config.PIXABAY_API_KEY, "q": query,
        "per_page": 25, "safesearch": "true"})
    hits = data.get("hits", [])
    hits = [h for h in hits if h.get("duration", 0) >= 6] or hits
    out: List[Hit] = []
    for h in hits:
        def dl(dest: Path, h=h) -> Path:
            variants = sorted(
                (v for v in h.get("videos", {}).values() if v and v.get("url")),
                key=lambda v: v.get("width") or 0)
            chosen = next((v for v in variants if (v.get("width") or 0) >= min_width),
                          variants[-1] if variants else None)
            if not chosen:
                raise RuntimeError("no downloadable variant")
            return _download(chosen["url"], dest,
                             cache_key=f"pixabay-video:{h.get('id')}:{chosen['url']}")
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
    data = media_cache.search_json(PEXELS_VIDEO_API,
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": 25})
    vids = data.get("videos", [])
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
            return _download(pick["link"], dest, timeout=300,
                             cache_key=f"pexels-video:{v.get('id')}:{pick['link']}")
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
    data = media_cache.search_json(PIXABAY_IMAGE_API, params={
        "key": config.PIXABAY_IMAGE_API_KEY, "q": query, "per_page": 25,
        "safesearch": "true", "orientation": "horizontal",
        "image_type": "photo"})
    out: List[Hit] = []
    for h in data.get("hits", []):
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        out.append(Hit(f"i:{h.get('id')}", _score(query, h.get("tags", "")),
                       h.get("tags", ""), "photo", 0,
                       h.get("webformatURL") or h.get("previewURL"),
                       lambda dest, url=url: _download(url, dest, kind="photo")))
    return out


def _pexels_photos(query: str) -> List[Hit]:
    if not config.PEXELS_API_KEY:
        return []
    data = media_cache.search_json(PEXELS_PHOTO_API,
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": 25,
                             "orientation": "landscape"})
    out: List[Hit] = []
    for p in data.get("photos", []):
        src = p.get("src") or {}
        url = src.get("large2x") or src.get("large")
        if not url:
            continue
        out.append(Hit(f"pxi:{p.get('id')}", _score(query, p.get("alt", "")),
                       p.get("alt", ""), "photo", 0, src.get("medium"),
                       lambda dest, url=url: _download(url, dest, kind="photo")))
    return out


# ───────────────────────── selection ────────────────────────────
def _gather(query: str, sources) -> List[Hit]:
    hits: List[Hit] = []
    token = cancel.current()
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [(source, executor.submit(cancel.adopt(token, source), query))
                   for source in sources]
        # Preserve source order for deterministic tie-breaking and diversity.
        for source, future in pending:
            try:
                hits += future.result()
            except cancel.JobCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                # Request exceptions may include Pixabay's key in the URL.
                print(f"[footage] {source.__name__} error for '{query}': {type(e).__name__}")
    return hits


def _record_pick(hit: Hit, dest: Path, narration: str, query: str, smart: str):
    artifacts.write(dest.with_suffix(".stock.json"), {
        "version": 1, "nid": hit.nid, "meta": hit.meta,
        "query": query, "mode": smart, "thumb": hit.thumb,
        "narration_hash": artifacts.digest(narration),
        "output": artifacts.signature(dest)})


def restore_used(beats):
    used = {}
    for beat in beats:
        if not beat.get("media"):
            continue
        path = Path(beat["media"])
        row = artifacts.read(path.with_suffix(".stock.json"))
        if row.get("nid") and row.get("output") == artifacts.signature(path):
            used[row["nid"]] = used.get(row["nid"], 0) + 1
    return used


@telemetry.timed("stock_selection")
def _select(hits: List[Hit], dest: Path, min_score: float,
            used: Optional[Dict[str, int]], narration: str, query: str,
            smart: str, max_repeats: int = 1) -> Optional[Path]:
    """Pick and download one hit. Smart modes ask the curator; if the judge
    is unavailable (network/LLM failure) we degrade to keyword scoring so a
    render never dies on the choosing step."""
    hits = [h for h in hits
            if not (used is not None and used.get(h.nid, 0) >= max_repeats)]
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
            except cancel.JobCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[footage] download failed for {h.nid}: {e}")
                pool.remove(h)
                hits.remove(h)  # don't retry the same failed file in keyword fallback
                if not pool:
                    return None
                continue
            if used is not None:
                used[h.nid] = used.get(h.nid, 0) + 1
            _record_pick(h, path, narration, query, smart)
            return path
    for h in hits:
        if h.score < min_score:
            return None
        try:
            path = h.download(dest)
        except cancel.JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[footage] download failed for {h.nid}: {e}")
            continue
        if used is not None:
            used[h.nid] = used.get(h.nid, 0) + 1
        _record_pick(h, path, narration, query, smart)
        return path
    return None


@telemetry.timed("stock_search")
def candidates(query: str, kind: str, min_width: int = 1280):
    cancel.check()
    if kind == "stock_image":
        return _gather(query, (_pixabay_photos, _pexels_photos))
    return _gather(query, (lambda q: _pixabay_videos(q, min_width),
                          lambda q: _pexels_videos(q, min_width)))


class Prefetch:
    """Only candidate searches overlap; selection/reservation stays ordered."""
    def __init__(self, beats, min_width):
        self.beats, self.width = beats, min_width
        self.window = max(1, min(6, int(config.get("STOCK_PREFETCH_BEATS", "3"))))
        self.pool = ThreadPoolExecutor(max_workers=self.window, thread_name_prefix="stock-prefetch")
        self.pending = {}
        self.token = cancel.current()

    def at(self, index):
        cancel.check()
        for i in range(index, min(len(self.beats), index + self.window)):
            b = self.beats[i]
            if (i not in self.pending and b.get("type") in ("stock", "stock_image")
                    and not b.get("media") and not b.get("_skip_stock")):
                self.pending[i] = self.pool.submit(cancel.adopt(self.token, candidates),
                                                   b.get("query", ""), b["type"], self.width)
        return self.pending.pop(index, None)

    def close(self):
        for future in self.pending.values():
            future.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)


def _alternatives(query):
    words = query.split()
    # One simpler search retaining the subject; never invent a specific part.
    plain = [w for w in words if w.casefold() not in
             {"cinematic", "beautiful", "dramatic", "closeup", "footage", "video", "shot"}]
    alternate = " ".join(plain)
    return [alternate] if alternate and alternate != query else []


def fetch_stock(query: str, dest: Path, min_width: int = 1280,
                min_score: float = 0.4, used: Optional[Dict[str, int]] = None,
                narration: str = "", smart: str = "off",
                max_repeats: int = 1, prefetched=None) -> Optional[Path]:
    """Best-matching stock VIDEO across both providers, or None."""
    hits = prefetched.result() if prefetched is not None else candidates(query, "stock", min_width)
    found = _select(hits, dest, min_score, used, narration, query, smart, max_repeats)
    for alternative in _alternatives(query) if found is None else []:
        found = _select(candidates(alternative, "stock", min_width), dest, min_score,
                        used, narration, alternative, smart, max_repeats)
        if found:
            break
    return found


def fetch_stock_image(query: str, dest: Path, min_score: float = 0.4,
                      used: Optional[Dict[str, int]] = None, narration: str = "",
                      smart: str = "off", max_repeats: int = 1, prefetched=None) -> Optional[Path]:
    """Best-matching stock PHOTO across both providers (Ken Burns at render)."""
    hits = prefetched.result() if prefetched is not None else candidates(query, "stock_image")
    return _select(hits, dest, min_score, used, narration, query, smart,
                   max_repeats)
