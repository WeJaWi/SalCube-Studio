"""Terra-assisted pre-production research for research-gated profiles."""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

import requests

from . import llm


MODEL = "gpt-5.6-terra"
REDDIT_SEARCH_URLS = (
    "https://www.reddit.com/search.json",
    "https://old.reddit.com/search.json",
)
REDDIT_HTML_HOST = "https://id.reddit.com"
REDDIT_HEADERS = {
    "User-Agent": "DmiqwiStudio/1.0 (private editorial research)",
    "Accept": "application/json",
}
REDDIT_SUBREDDITS = (
    "BestofRedditorUpdates", "TrueOffMyChest", "relationship_advice",
    "AmItheAsshole", "pettyrevenge", "ProRevenge", "EntitledPeople",
)


def _clean_reddit_text(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"(?m)^\s*(?:edit|update)\s*[:\-]", "\nUpdate: ", value,
                   flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _html_fragment_text(fragment: str) -> str:
    fragment = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>|</blockquote>",
                      "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return _clean_reddit_text(fragment)


def _main_post_text(page: str) -> str:
    """Extract the first submission's Markdown-rendered body from old Reddit."""
    thing = page.find("thing id-t3")
    body = page.find("usertext-body may-blank-within md-container", thing)
    opening = page.find('<div class="md">', body)
    if min(thing, body, opening) < 0:
        return ""
    start = opening + len('<div class="md">')
    depth = 1
    end = start
    for token in re.finditer(r"(?i)<div\b[^>]*>|</div\s*>", page[start:]):
        if token.group(0).lower().startswith("<div"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end = start + token.start()
                break
    return _html_fragment_text(page[start:end]) if end > start else ""


def _reddit_html_search(query: str, subreddits: list[str]) -> list[dict[str, Any]]:
    scoped = " OR ".join(f"subreddit:{name}" for name in subreddits[:5])
    response = requests.get(
        f"{REDDIT_HTML_HOST}/search/",
        params={"q": f"({query}) ({scoped})", "sort": "top", "t": "all"},
        headers=REDDIT_HEADERS, timeout=30)
    response.raise_for_status()
    links = re.findall(
        r'<a href="(https://[^\"]+/r/[^\"]+/comments/[^\"]+)" '
        r'class="search-title may-blank"[^>]*>(.*?)</a>',
        response.text, flags=re.IGNORECASE | re.DOTALL)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url, raw_title in links:
        parsed = urlparse(html.unescape(url))
        canonical_path = parsed.path.rstrip("/")
        if canonical_path in seen:
            continue
        seen.add(canonical_path)
        try:
            page = requests.get(f"{REDDIT_HTML_HOST}{canonical_path}",
                                headers=REDDIT_HEADERS, timeout=30)
            page.raise_for_status()
            body = _main_post_text(page.text)
        except Exception:  # noqa: BLE001
            continue
        if len(body) < 900 or "over18" in page.text[30000:36000].lower():
            continue
        match = re.match(r"/r/([^/]+)/", canonical_path, flags=re.IGNORECASE)
        candidates.append({
            "title": _html_fragment_text(raw_title), "body": body[:24000],
            "subreddit": match.group(1) if match else "unknown",
            "score": 0, "comments": 0, "permalink": canonical_path,
        })
        if len(candidates) >= 12:
            break
    return candidates


def _reddit_search(theme: str) -> list[dict[str, Any]]:
    query_plan = llm.chat_json(
        "Turn the user's requested Reddit-story theme into one concise Reddit "
        "search query. Favor a detailed first-person post with conflict, a clear "
        "turning point, and an ending or update. Do not invent a post. Return ONLY "
        "JSON with search_query and preferred_subreddits (up to 4 names).\n\n"
        f"Theme: {theme}", MODEL, max_tokens=500, attempts=2, retries=1)
    query = str(query_plan.get("search_query") or theme).strip()[:180]
    wanted = [str(x).strip() for x in
              (query_plan.get("preferred_subreddits") or []) if str(x).strip()]
    allowed = {x.lower(): x for x in REDDIT_SUBREDDITS}
    subreddits = [allowed[x.lower()] for x in wanted if x.lower() in allowed]
    if not subreddits:
        subreddits = list(REDDIT_SUBREDDITS)
    # Reddit's JSON API requires OAuth from this host, but its public old-style
    # HTML remains accessible. Terra still creates the query and later judges
    # the candidates; this collector only retrieves the real source text.
    try:
        html_candidates = _reddit_html_search(query, subreddits)
        if html_candidates:
            return html_candidates
    except Exception:  # noqa: BLE001
        pass
    scoped = " OR ".join(f"subreddit:{name}" for name in subreddits[:5])
    params = {
        "q": f"({query}) ({scoped})", "sort": "top", "t": "all",
        "type": "link", "limit": 50, "raw_json": 1,
    }
    last_error: Exception | None = None
    for url in REDDIT_SEARCH_URLS:
        try:
            response = requests.get(url, params=params, headers=REDDIT_HEADERS,
                                    timeout=30)
            response.raise_for_status()
            children = ((response.json().get("data") or {}).get("children") or [])
            candidates = []
            for child in children:
                data = child.get("data") or {}
                body = _clean_reddit_text(data.get("selftext") or "")
                if (not data.get("is_self") or data.get("over_18")
                        or len(body) < 900):
                    continue
                candidates.append({
                    "title": str(data.get("title") or "").strip(),
                    "body": body[:24000],
                    "subreddit": str(data.get("subreddit") or ""),
                    "score": int(data.get("score") or 0),
                    "comments": int(data.get("num_comments") or 0),
                    "permalink": str(data.get("permalink") or ""),
                })
            if candidates:
                return candidates[:15]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(
        "Reddit search returned no usable public first-person post"
        + (f" ({last_error})" if last_error else ""))


def _pick_reddit_story(theme: str, candidates: list[dict[str, Any]]) -> dict:
    summaries = "\n\n".join(
        f"[{i}] r/{c['subreddit']} | score {c['score']} | "
        f"{c['comments']} comments\nTITLE: {c['title']}\n"
        f"EXCERPT: {c['body'][:1600]}"
        for i, c in enumerate(candidates)
    )
    picked = llm.chat_json(
        "Select exactly one real Reddit post from the numbered candidates for "
        "a long narrated adaptation. It must strongly match the requested theme, "
        "have enough concrete events for a faithful expansion, and avoid explicit "
        "sexual content, self-harm instructions, hate, or identifying private "
        "details. Return ONLY JSON: selected_index (integer), editorial_angle, "
        "and three distinct high-click YouTube titles. Titles must be truthful, "
        "English, under 100 characters, and may not claim events absent from the "
        "post.\n\n"
        f"Requested theme: {theme}\n\nCANDIDATES:\n{summaries}",
        MODEL, max_tokens=1200, attempts=2, retries=1, timeout=120)
    try:
        index = int(picked.get("selected_index"))
    except (TypeError, ValueError):
        index = 0
    if not 0 <= index < len(candidates):
        index = 0
    story = dict(candidates[index])
    story["editorial_angle"] = str(picked.get("editorial_angle") or "").strip()
    story["titles"] = _titles(picked.get("titles"))
    return story


def _titles(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        raw = list(raw.values())
    values = []
    for item in raw if isinstance(raw, list) else []:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" \"'")[:120]
        if text and text.casefold() not in {x.casefold() for x in values}:
            values.append(text)
    if len(values) != 3:
        raise RuntimeError("Terra did not return exactly three distinct titles")
    return values


def reddit_package(theme: str) -> dict:
    story = _pick_reddit_story(theme, _reddit_search(theme))
    return {
        "titles": story.pop("titles"),
        "research_brief": (
            "Adapt this one public first-person Reddit account into an original, "
            "anonymized long-form narration. Preserve the event order, conflict, "
            "turning point, and outcome. You may deepen scene transitions, inner "
            "reflection, stakes, and lessons, but never invent new accusations, "
            "crimes, diagnoses, evidence, or outcomes. Do not mention Reddit, "
            "usernames, votes, or the source in the spoken narration."
            + (f" Editorial angle: {story['editorial_angle']}"
               if story.get("editorial_angle") else "")
        ),
        "source_material": f"ORIGINAL POST TITLE: {story['title']}\n\n{story['body']}",
        # Kept locally for editorial traceability; never inserted in the DOCX.
        "source_metadata": {
            "platform": "reddit", "subreddit": story["subreddit"],
            "permalink": story["permalink"], "score": story["score"],
        },
    }


def astrology_package(theme: str) -> dict:
    package = llm.chat_json(
        "Research and package one long-form English astrology video inspired by "
        "thoughtful spiritual channels, without copying their wording. Treat "
        "astrology as symbolic/spiritual entertainment, not established science. "
        "Use the requested theme as the boundary. Never invent a current transit, "
        "astronomical date, historical quotation, or scientific result; if the "
        "theme supplies no verifiable date, make the treatment evergreen. Return "
        "ONLY JSON with research_brief (detailed concepts, structure, cautions, "
        "practical reflection points) and titles (exactly 3 distinct high-click, "
        "truthful English YouTube titles under 100 characters).\n\n"
        f"Requested theme: {theme}", MODEL, max_tokens=3500, attempts=2,
        retries=1, timeout=180)
    return {
        "titles": _titles(package.get("titles")),
        "research_brief": str(package.get("research_brief") or "").strip(),
        "source_material": "",
        "source_metadata": {"platform": "terra-research"},
    }


def build_package(profile_name: str, theme: str) -> dict:
    if profile_name == "reddit":
        return reddit_package(theme)
    if profile_name == "astrology-search":
        return astrology_package(theme)
    raise RuntimeError(f"profile {profile_name} does not support research")
