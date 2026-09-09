#!/usr/bin/env python3
"""spotify-show-stats — catalogue statistics for Spotify shows (podcasts).

Pulls a show and its complete episode list from the public Spotify Web API
with an app-only (client-credentials) token, then turns that into a report:
runtime distribution, release cadence, publishing rhythm, and the episodes
at the extremes.

What this deliberately does NOT report: plays, listeners, followers,
completion rate, or any other audience metric. Spotify exposes those only to
a show's own owner inside Spotify for Creators, and there is no public API
for them — see README.md. Everything here is derived from public catalogue
metadata, so it works for ANY show, not just your own.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html as html_mod
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests

import nowplaying

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PAGE_SIZE = 50          # Spotify's maximum for the episodes endpoint
MAX_RETRIES = 5

# Minute boundaries for the runtime histogram. Chosen to separate the shapes
# podcasts actually cluster into (shorts, commute-length, long-form).
DURATION_BUCKETS: Sequence[Tuple[str, float, float]] = (
    ("under 10 min", 0, 10),
    ("10-20 min", 10, 20),
    ("20-30 min", 20, 30),
    ("30-45 min", 30, 45),
    ("45-60 min", 45, 60),
    ("60-90 min", 60, 90),
    ("90 min+", 90, float("inf")),
)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ─────────────────────────────────────────────────────────────
#  Environment / credentials
# ─────────────────────────────────────────────────────────────

def load_env_files(extra: Optional[Path] = None) -> None:
    """Populate os.environ from .env files, without pulling in a dependency.

    Looks in this tool's directory first, then the repository root (so the
    keys can live in the studio's existing .env), then any --env-file. A
    variable already present in the real environment always wins.
    """
    here = Path(__file__).resolve().parent
    candidates = [here / ".env", here.parent / ".env"]
    if extra is not None:
        candidates.append(Path(extra).expanduser())

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


class SpotifyError(RuntimeError):
    """Any failure talking to Spotify, already phrased for a human."""


# ─────────────────────────────────────────────────────────────
#  API client
# ─────────────────────────────────────────────────────────────

class Spotify:
    """Thin client for the handful of endpoints this tool needs.

    Client-credentials tokens carry no user context, which is why every
    show/episode call must pass an explicit `market`: without one Spotify
    rejects the request rather than guessing a region.
    """

    def __init__(self, client_id: str, client_secret: str,
                 market: str = "US", timeout: int = 30,
                 quiet: bool = False) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.market = market.upper()
        self.timeout = timeout
        self.quiet = quiet
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._token_expires = 0.0

    # -- auth ------------------------------------------------

    def _token_header(self, force: bool = False) -> Dict[str, str]:
        if force or not self._token or time.time() > self._token_expires:
            basic = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode("utf-8")
            ).decode("ascii")
            try:
                resp = self._session.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {basic}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise SpotifyError(f"could not reach Spotify accounts: {exc}") from exc
            if resp.status_code != 200:
                raise SpotifyError(
                    "Spotify rejected the client credentials "
                    f"(HTTP {resp.status_code}). Check SPOTIFY_CLIENT_ID / "
                    "SPOTIFY_CLIENT_SECRET against your app at "
                    "https://developer.spotify.com/dashboard"
                )
            payload = resp.json()
            self._token = payload["access_token"]
            # Refresh a minute early so a long pagination run cannot expire
            # mid-flight.
            self._token_expires = time.time() + float(payload.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    # -- transport -------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        for attempt in range(MAX_RETRIES):
            headers = self._token_header(force=attempt > 0 and self._last_status == 401)
            try:
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise SpotifyError(f"network error calling {url}: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            self._last_status = resp.status_code
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "2")) + 1
                self._note(f"rate limited by Spotify, waiting {wait:.0f}s…")
                time.sleep(wait)
                continue
            if resp.status_code == 401 and attempt < MAX_RETRIES - 1:
                continue  # token expired mid-run; the next pass forces a refresh
            if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise SpotifyError(self._explain_error(resp, url))
        raise SpotifyError(f"gave up after {MAX_RETRIES} attempts calling {url}")

    _last_status = 0

    def _explain_error(self, resp: "requests.Response", url: str) -> str:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = resp.text[:200]
        if resp.status_code == 404:
            return (f"not found (HTTP 404) at {url}. A show can also 404 when it "
                    f"is not licensed in the market you asked for "
                    f"(--market {self.market}); try another, e.g. --market DE.")
        if resp.status_code == 400 and "market" in message.lower():
            return f"Spotify needs a valid market code; '{self.market}' was refused."
        return f"Spotify returned HTTP {resp.status_code}: {message or resp.reason}"

    def _note(self, text: str) -> None:
        if not self.quiet:
            print(f"  … {text}", file=sys.stderr)

    # -- endpoints -------------------------------------------

    def search_shows(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        data = self.get("/search", {"q": query, "type": "show",
                                    "market": self.market, "limit": limit})
        items = (data.get("shows") or {}).get("items") or []
        return [item for item in items if item]

    def show(self, show_id: str) -> Dict[str, Any]:
        return self.get(f"/shows/{show_id}", {"market": self.market})

    def episodes(self, show_id: str, max_episodes: Optional[int] = None,
                 total_hint: Optional[int] = None) -> List[Dict[str, Any]]:
        """Page through every episode, newest first (Spotify's own order)."""
        collected: List[Dict[str, Any]] = []
        offset = 0
        while True:
            data = self.get(f"/shows/{show_id}/episodes",
                            {"market": self.market, "limit": PAGE_SIZE, "offset": offset})
            # Spotify occasionally returns null placeholders for episodes that
            # are region-locked or withdrawn; they carry no data at all.
            batch = [item for item in (data.get("items") or []) if item]
            collected.extend(batch)
            self._note(f"fetched {len(collected)}"
                       f"{'/' + str(total_hint) if total_hint else ''} episodes")
            if max_episodes and len(collected) >= max_episodes:
                return collected[:max_episodes]
            if not data.get("next"):
                return collected
            offset += PAGE_SIZE


# ─────────────────────────────────────────────────────────────
#  Input parsing
# ─────────────────────────────────────────────────────────────

SHOW_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def extract_show_id(value: str) -> Optional[str]:
    """Accept a raw id, a spotify: URI, or any open.spotify.com/show URL."""
    value = value.strip()
    if SHOW_ID_RE.match(value):
        return value
    match = re.search(r"(?:show[:/])([A-Za-z0-9]{22})", value)
    return match.group(1) if match else None


def parse_release_date(raw: str, precision: str) -> Optional[date]:
    """Return a date only when Spotify actually knows the day.

    Month- and year-precision releases are common on old back catalogues.
    Counting them as "the 1st" would invent a cadence that never happened,
    so they are excluded from gap analysis and weekday stats instead.
    """
    if not raw or precision != "day":
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_year(raw: str) -> Optional[int]:
    try:
        return int(str(raw)[:4])
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
#  Analysis
# ─────────────────────────────────────────────────────────────

def normalise_episode(raw: Dict[str, Any]) -> Dict[str, Any]:
    duration_ms = raw.get("duration_ms") or 0
    precision = raw.get("release_date_precision") or ""
    release_raw = raw.get("release_date") or ""
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or "(untitled)",
        "release_date": release_raw,
        "release_precision": precision,
        "date": parse_release_date(release_raw, precision),
        "year": parse_year(release_raw),
        "duration_ms": duration_ms,
        "minutes": duration_ms / 60000.0,
        "explicit": bool(raw.get("explicit")),
        "language": raw.get("language") or "",
        "url": (raw.get("external_urls") or {}).get("spotify", ""),
        "description": (raw.get("description") or "").strip(),
    }


def _stats_block(values: Sequence[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None,
                "max": None, "stdev": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "stdev": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
    }


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _months_back(anchor: date, count: int) -> List[str]:
    keys: List[str] = []
    year, month = anchor.year, anchor.month
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def describe_regularity(gaps: Sequence[int]) -> str:
    """Turn gap scatter into a plain-language rhythm label."""
    if len(gaps) < 3:
        return "not enough dated episodes to judge"
    median = statistics.median(gaps)
    if median <= 0:
        return "several episodes share release dates (batch drops)"
    spread = statistics.pstdev(gaps) / median
    if spread < 0.35:
        shape = "very consistent"
    elif spread < 0.8:
        shape = "fairly consistent"
    elif spread < 1.6:
        shape = "irregular"
    else:
        shape = "very irregular (bursts and long silences)"
    if median <= 1.5:
        rhythm = "roughly daily"
    elif median <= 4:
        rhythm = "several times a week"
    elif median <= 10:
        rhythm = "roughly weekly"
    elif median <= 18:
        rhythm = "roughly fortnightly"
    elif median <= 45:
        rhythm = "roughly monthly"
    else:
        rhythm = "less than monthly"
    return f"{rhythm}, {shape}"


def build_report(show: Dict[str, Any], raw_episodes: Sequence[Dict[str, Any]],
                 market: str) -> Dict[str, Any]:
    episodes = [normalise_episode(raw) for raw in raw_episodes]
    dated = sorted([ep for ep in episodes if ep["date"]], key=lambda ep: ep["date"])
    today = datetime.now(timezone.utc).date()

    minutes = [ep["minutes"] for ep in episodes if ep["duration_ms"]]
    duration = _stats_block(minutes)
    total_minutes = sum(minutes)

    # Cadence: consecutive gaps in days across day-precision releases only.
    gaps = [(later["date"] - earlier["date"]).days
            for earlier, later in zip(dated, dated[1:])]
    gap_stats = _stats_block([float(g) for g in gaps])

    first_day = dated[0]["date"] if dated else None
    last_day = dated[-1]["date"] if dated else None
    span_days = (last_day - first_day).days if first_day and last_day else 0

    recent = {
        "last_30_days": sum(1 for ep in dated if (today - ep["date"]).days <= 30),
        "last_90_days": sum(1 for ep in dated if (today - ep["date"]).days <= 90),
        "last_365_days": sum(1 for ep in dated if (today - ep["date"]).days <= 365),
    }

    weekday_counts = Counter(ep["date"].weekday() for ep in dated)
    yearly = Counter(ep["year"] for ep in episodes if ep["year"])
    monthly_counter = Counter(_month_key(ep["date"]) for ep in dated)
    month_keys = _months_back(today, 24)
    monthly = [(key, monthly_counter.get(key, 0)) for key in month_keys]

    buckets = []
    for label, low, high in DURATION_BUCKETS:
        count = sum(1 for m in minutes if low <= m < high)
        buckets.append({"label": label, "count": count})

    by_length = sorted([ep for ep in episodes if ep["duration_ms"]],
                       key=lambda ep: ep["minutes"])
    newest = sorted(dated, key=lambda ep: ep["date"], reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": market,
        "show": {
            "id": show.get("id"),
            "name": show.get("name"),
            "publisher": show.get("publisher"),
            "description": (show.get("description") or "").strip(),
            "languages": show.get("languages") or [],
            "media_type": show.get("media_type"),
            "explicit": bool(show.get("explicit")),
            "total_episodes_reported": show.get("total_episodes"),
            "url": (show.get("external_urls") or {}).get("spotify", ""),
            "image": (show.get("images") or [{}])[0].get("url", ""),
        },
        "totals": {
            "episodes_fetched": len(episodes),
            "episodes_dated": len(dated),
            "undated_episodes": len(episodes) - len(dated),
            "total_minutes": total_minutes,
            "total_hours": total_minutes / 60.0,
            "explicit_episodes": sum(1 for ep in episodes if ep["explicit"]),
            "languages": sorted({ep["language"] for ep in episodes if ep["language"]}),
            "first_release": first_day.isoformat() if first_day else None,
            "last_release": last_day.isoformat() if last_day else None,
            "catalogue_span_days": span_days,
            "days_since_last": (today - last_day).days if last_day else None,
            "episodes_per_month": (len(dated) / (span_days / 30.44)) if span_days else None,
        },
        "duration_minutes": duration,
        "cadence_days": gap_stats,
        "cadence_label": describe_regularity(gaps),
        "recent": recent,
        "histograms": {
            "weekday": [{"label": WEEKDAYS[i], "count": weekday_counts.get(i, 0)}
                        for i in range(7)],
            "yearly": [{"label": str(year), "count": yearly[year]}
                       for year in sorted(yearly)],
            "monthly": [{"label": key, "count": count} for key, count in monthly],
            "duration": buckets,
        },
        "extremes": {
            "longest": list(reversed(by_length[-5:])),
            "shortest": by_length[:5],
            "newest": newest[:10],
        },
        "episodes": episodes,
    }


# ─────────────────────────────────────────────────────────────
#  Formatting helpers
# ─────────────────────────────────────────────────────────────

class Palette:
    """ANSI styling that silently no-ops when output is piped."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap(text, "1")

    def dim(self, text: str) -> str:
        return self._wrap(text, "2")

    def green(self, text: str) -> str:
        return self._wrap(text, "32")

    def cyan(self, text: str) -> str:
        return self._wrap(text, "36")

    def yellow(self, text: str) -> str:
        return self._wrap(text, "33")


def fmt_minutes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f} min"
    hours, minutes = divmod(int(round(value)), 60)
    return f"{hours}h {minutes:02d}m"


def fmt_hours(hours: float) -> str:
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours:.0f} h ({hours / 24:.1f} days)"


def fmt_days(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f} d"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def bar(count: int, peak: int, width: int = 28) -> str:
    if peak <= 0:
        return ""
    filled = int(round((count / peak) * width))
    return "█" * filled + "·" * (width - filled)


def sparkline(counts: Sequence[int]) -> str:
    blocks = " ▁▂▃▄▅▆▇█"
    peak = max(counts) if counts else 0
    if peak <= 0:
        return " " * len(counts)
    return "".join(blocks[min(8, int(round(value / peak * 8)))] for value in counts)


def meta_line(*parts: Optional[str]) -> str:
    """Join the parts that exist with a middot. Spotify no longer returns
    `publisher` on shows, so any of these can legitimately be missing."""
    return "  ·  ".join(part for part in parts if part)


def truncate(text: str, width: int) -> str:
    """For prose: collapse whitespace, then cut."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def clip(text: str, width: int) -> str:
    """For aligned columns: cut only — internal padding is load-bearing."""
    return text if len(text) <= width else text[: width - 1] + "…"


# ─────────────────────────────────────────────────────────────
#  Terminal report
# ─────────────────────────────────────────────────────────────

def render_terminal(report: Dict[str, Any], pal: Palette, top: int = 10) -> str:
    show = report["show"]
    totals = report["totals"]
    duration = report["duration_minutes"]
    cadence = report["cadence_days"]
    out: List[str] = []
    rule = "─" * 74

    def section(title: str) -> None:
        out.append("")
        out.append(pal.bold(title))
        out.append(pal.dim(rule))

    out.append(pal.dim(rule))
    out.append(pal.bold(pal.green(show["name"] or "(unknown show)")))
    out.append(meta_line(show["publisher"],
                         ", ".join(show["languages"]),
                         f"{show['media_type']}" if show["media_type"] else "",
                         f"market {report['market']}",
                         "explicit" if show["explicit"] else ""))
    if show["url"]:
        out.append(pal.dim(show["url"]))
    if show["description"]:
        out.append("")
        out.append(truncate(show["description"], 220))
    out.append(pal.dim(rule))

    section("CATALOGUE")
    reported = totals.get("episodes_fetched")
    claimed = show.get("total_episodes_reported")
    line = f"  Episodes analysed      {reported}"
    if claimed and claimed != reported:
        line += pal.dim(f"   (Spotify lists {claimed} for this market)")
    out.append(line)
    out.append(f"  Total runtime          {fmt_hours(totals['total_hours'])}")
    out.append(f"  First / last release   {totals['first_release'] or '—'}"
               f"  →  {totals['last_release'] or '—'}")
    if totals["catalogue_span_days"]:
        out.append(f"  Active span            {totals['catalogue_span_days']} days "
                   f"({totals['catalogue_span_days'] / 365.25:.1f} years)")
    if totals["days_since_last"] is not None:
        freshness = totals["days_since_last"]
        colour = pal.green if freshness <= 14 else (pal.yellow if freshness <= 60 else pal.dim)
        out.append(f"  Days since last drop   {colour(str(freshness))}")
    if totals["explicit_episodes"]:
        out.append(f"  Explicit episodes      {totals['explicit_episodes']}")
    if totals["undated_episodes"]:
        out.append(pal.dim(f"  Undated (month/year precision): {totals['undated_episodes']}"
                           " — excluded from cadence and weekday stats"))

    section("EPISODE LENGTH")
    out.append(f"  Median                 {fmt_minutes(duration['median'])}")
    out.append(f"  Mean                   {fmt_minutes(duration['mean'])}")
    out.append(f"  Shortest / longest     {fmt_minutes(duration['min'])}"
               f"  →  {fmt_minutes(duration['max'])}")
    out.append("")
    peak = max((b["count"] for b in report["histograms"]["duration"]), default=0)
    for bucket in report["histograms"]["duration"]:
        out.append(f"  {bucket['label']:<14}{pal.cyan(bar(bucket['count'], peak))} "
                   f"{bucket['count']}")

    section("RELEASE CADENCE")
    out.append(f"  Rhythm                 {pal.cyan(report['cadence_label'])}")
    out.append(f"  Median gap             {fmt_days(cadence['median'])}")
    out.append(f"  Mean gap               {fmt_days(cadence['mean'])}")
    out.append(f"  Shortest / longest gap {fmt_days(cadence['min'])}  →  "
               f"{fmt_days(cadence['max'])}")
    if totals["episodes_per_month"]:
        out.append(f"  Episodes per month     {totals['episodes_per_month']:.1f} (lifetime avg)")
    out.append(f"  Released last 30/90/365d  {report['recent']['last_30_days']} / "
               f"{report['recent']['last_90_days']} / {report['recent']['last_365_days']}")

    weekdays = report["histograms"]["weekday"]
    if any(day["count"] for day in weekdays):
        out.append("")
        peak = max(day["count"] for day in weekdays)
        for day in weekdays:
            out.append(f"  {day['label']:<14}{pal.cyan(bar(day['count'], peak))} "
                       f"{day['count']}")

    monthly = report["histograms"]["monthly"]
    if any(month["count"] for month in monthly):
        section("LAST 24 MONTHS")
        counts = [month["count"] for month in monthly]
        out.append("  " + pal.cyan(sparkline(counts)))
        # Pin the end label to the sparkline's last column so the axis reads true.
        start, end = monthly[0]["label"], monthly[-1]["label"]
        pad = max(1, len(counts) - len(start) - len(end))
        out.append(pal.dim(f"  {start}{' ' * pad}{end}   peak {max(counts)}/month"))

    yearly = report["histograms"]["yearly"]
    if len(yearly) > 1:
        section("EPISODES PER YEAR")
        peak = max(year["count"] for year in yearly)
        for year in yearly:
            out.append(f"  {year['label']:<14}{pal.cyan(bar(year['count'], peak))} "
                       f"{year['count']}")

    if top > 0 and report["extremes"]["newest"]:
        section(f"NEWEST {min(top, len(report['extremes']['newest']))} EPISODES")
        for ep in report["extremes"]["newest"][:top]:
            out.append(f"  {ep['release_date']}  {fmt_minutes(ep['minutes']):>8}  "
                       f"{truncate(ep['name'], 46)}")

    if report["extremes"]["longest"]:
        section("LONGEST / SHORTEST")
        for ep in report["extremes"]["longest"][:3]:
            out.append(f"  ▲ {fmt_minutes(ep['minutes']):>8}  {truncate(ep['name'], 52)}")
        for ep in report["extremes"]["shortest"][:3]:
            out.append(f"  ▼ {fmt_minutes(ep['minutes']):>8}  {truncate(ep['name'], 52)}")

    out.append("")
    out.append(pal.dim("Public catalogue data only — Spotify does not expose plays, "
                       "listeners or followers for shows via its API."))
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────
#  Exports
# ─────────────────────────────────────────────────────────────

def write_json(report: Dict[str, Any], path: Path) -> None:
    def default(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(f"not serialisable: {type(value)}")

    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=default),
                    encoding="utf-8")


def write_csv(report: Dict[str, Any], path: Path) -> None:
    fields = ["release_date", "name", "duration_minutes", "explicit", "language",
              "id", "url", "description"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for ep in report["episodes"]:
            writer.writerow([
                ep["release_date"], ep["name"], f"{ep['minutes']:.2f}",
                "yes" if ep["explicit"] else "no", ep["language"], ep["id"],
                ep["url"], " ".join(ep["description"].split()),
            ])


def _html_bars(rows: Sequence[Dict[str, Any]]) -> str:
    peak = max((row["count"] for row in rows), default=0) or 1
    cells = []
    for row in rows:
        width = row["count"] / peak * 100
        cells.append(
            f'<tr><th>{html_mod.escape(str(row["label"]))}</th>'
            f'<td class="bar"><span style="width:{width:.1f}%"></span></td>'
            f'<td class="num">{row["count"]}</td></tr>'
        )
    return "".join(cells)


def render_html(report: Dict[str, Any]) -> str:
    show = report["show"]
    totals = report["totals"]
    duration = report["duration_minutes"]
    cadence = report["cadence_days"]
    esc = html_mod.escape

    def stat(label: str, value: str) -> str:
        return (f'<div class="stat"><span class="label">{esc(label)}</span>'
                f'<span class="value">{esc(value)}</span></div>')

    stats = "".join([
        stat("Episodes", str(totals["episodes_fetched"])),
        stat("Total runtime", fmt_hours(totals["total_hours"])),
        stat("Median length", fmt_minutes(duration["median"])),
        stat("Median gap", fmt_days(cadence["median"])),
        stat("Episodes / month", f"{totals['episodes_per_month']:.1f}"
             if totals["episodes_per_month"] else "—"),
        stat("Days since last", str(totals["days_since_last"])
             if totals["days_since_last"] is not None else "—"),
    ])

    def episode_row(ep: Dict[str, Any]) -> str:
        link = '<a href="{}">open</a>'.format(esc(ep["url"])) if ep["url"] else ""
        explicit = "yes" if ep["explicit"] else ""
        return ("<tr><td>{}</td><td>{}</td><td class=\"num\">{:.0f}</td>"
                "<td>{}</td><td>{}</td></tr>").format(
                    esc(ep["release_date"]), esc(ep["name"]), ep["minutes"],
                    explicit, link)

    rows = "".join(episode_row(ep) for ep in report["episodes"])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(show['name'] or 'Show stats')} — Spotify show stats</title>
<style>
:root {{ --bg:#ffffff; --fg:#16181c; --muted:#6b7280; --line:#e5e7eb;
         --card:#f7f8fa; --accent:#1db954; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0e1013; --fg:#e9eaec; --muted:#9aa1ab; --line:#252a31;
           --card:#161a1f; --accent:#1ed760; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2.5rem 1.25rem; background:var(--bg); color:var(--fg);
        font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif; }}
main {{ max-width:920px; margin:0 auto; }}
h1 {{ font-size:1.9rem; margin:0 0 .25rem; }}
h2 {{ font-size:1rem; text-transform:uppercase; letter-spacing:.08em;
      color:var(--muted); margin:2.5rem 0 .75rem; }}
.sub {{ color:var(--muted); margin:0 0 1.5rem; }}
.desc {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:1rem; color:var(--muted); }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
          gap:.75rem; margin-top:1.5rem; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:.9rem 1rem; }}
.stat .label {{ display:block; font-size:.75rem; text-transform:uppercase;
                letter-spacing:.06em; color:var(--muted); }}
.stat .value {{ display:block; font-size:1.25rem; font-weight:600; margin-top:.2rem;
                white-space:nowrap; }}
table {{ width:100%; border-collapse:collapse; }}
.charts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:2rem; }}
h3 {{ font-size:.85rem; font-weight:600; margin:0 0 .6rem; color:var(--fg); }}
.chart th {{ text-align:left; font-weight:500; color:var(--muted); white-space:nowrap;
             padding:.2rem .75rem .2rem 0; font-size:.85rem; }}
.bar {{ width:100%; }}
.bar span {{ display:block; height:12px; border-radius:3px; background:var(--accent);
             min-width:2px; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; padding-left:.6rem; }}
.episodes {{ overflow-x:auto; }}
.episodes table {{ min-width:640px; }}
.episodes th, .episodes td {{ border-bottom:1px solid var(--line); padding:.45rem .6rem;
                              text-align:left; vertical-align:top; }}
.episodes thead th {{ position:sticky; top:0; background:var(--bg); }}
a {{ color:var(--accent); }}
footer {{ margin-top:3rem; color:var(--muted); font-size:.85rem;
          border-top:1px solid var(--line); padding-top:1rem; }}
</style></head><body><main>
<h1>{esc(show['name'] or 'Unknown show')}</h1>
<p class="sub">{esc(meta_line(show['publisher'], ', '.join(show['languages']),
                              'market ' + report['market']))}
 · <a href="{esc(show['url'])}">open on Spotify</a></p>
<p class="desc">{esc(show['description'])}</p>
<div class="stats">{stats}</div>

<h2>Shape of the catalogue</h2>
<div class="charts">
  <section><h3>Episode length</h3>
    <table class="chart">{_html_bars(report['histograms']['duration'])}</table></section>
  <section><h3>Release weekday</h3>
    <table class="chart">{_html_bars(report['histograms']['weekday'])}</table></section>
</div>

<h2>Episodes per year</h2>
<table class="chart">{_html_bars(report['histograms']['yearly'])}</table>

<h2>Last 24 months</h2>
<table class="chart">{_html_bars(report['histograms']['monthly'])}</table>

<h2>All {totals['episodes_fetched']} episodes</h2>
<div class="episodes"><table>
<thead><tr><th>Released</th><th>Title</th><th class="num">Min</th>
<th>Explicit</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>

<footer>Generated {esc(report['generated_at'])} from the public Spotify Web API.
Play counts, listeners and followers are not available through that API —
they exist only in Spotify for Creators, for the show's own owner.</footer>
</main></body></html>"""


# ─────────────────────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────────────────────

def resolve_show(client: Spotify, target: str, pal: Palette) -> Dict[str, Any]:
    """Turn an id, URL, or free-text name into one show object."""
    show_id = extract_show_id(target)
    if show_id:
        return client.show(show_id)
    matches = client.search_shows(target, limit=5)
    if not matches:
        raise SpotifyError(f"no show on Spotify matches {target!r} in market "
                           f"{client.market}")
    best = matches[0]
    if not client.quiet:
        matched = meta_line(best.get("publisher"),
                            f"{best.get('total_episodes', '?')} episodes")
        print(pal.dim(f"  … matched \"{best.get('name')}\" — {matched} "
                      f"({best.get('id')})"), file=sys.stderr)
        if len(matches) > 1:
            others = ", ".join(f"{m.get('name')}" for m in matches[1:])
            print(pal.dim(f"  … other matches: {others}"), file=sys.stderr)
    return client.show(best["id"])


def cmd_search(client: Spotify, args: argparse.Namespace, pal: Palette) -> int:
    matches = client.search_shows(args.query, limit=args.limit)
    if not matches:
        print(f"No shows found for {args.query!r} in market {client.market}.")
        return 1
    print()
    for show in matches:
        print(f"  {pal.bold(show.get('name') or '—')}")
        print("    " + meta_line(
            show.get("publisher"),
            f"{show.get('total_episodes', '?')} episodes",
            ", ".join(show.get("languages") or []),
            "explicit" if show.get("explicit") else ""))
        print(f"    {pal.cyan(show.get('id') or '')}   "
              f"{pal.dim((show.get('external_urls') or {}).get('spotify', ''))}")
        print()
    return 0


def fetch_report(client: Spotify, target: str, args: argparse.Namespace,
                 pal: Palette) -> Dict[str, Any]:
    show = resolve_show(client, target, pal)
    episodes = client.episodes(show["id"], max_episodes=args.max_episodes,
                               total_hint=show.get("total_episodes"))
    return build_report(show, episodes, client.market)


def cmd_stats(client: Spotify, args: argparse.Namespace, pal: Palette) -> int:
    report = fetch_report(client, args.show, args, pal)
    print(render_terminal(report, pal, top=args.top))

    if args.json:
        write_json(report, Path(args.json))
        print(pal.dim(f"  JSON written to {args.json}"))
    if args.csv:
        write_csv(report, Path(args.csv))
        print(pal.dim(f"  CSV written to {args.csv}"))
    if args.html:
        Path(args.html).write_text(render_html(report), encoding="utf-8")
        print(pal.dim(f"  HTML report written to {args.html}"))
    return 0


def cmd_compare(client: Spotify, args: argparse.Namespace, pal: Palette) -> int:
    reports = [fetch_report(client, target, args, pal) for target in args.shows]
    name_width = max(len(r["show"]["name"] or "") for r in reports)
    name_width = min(max(name_width, 12), 34)

    header = (f"  {'Show':<{name_width}}  {'Eps':>5}  {'Hours':>7}  {'Median':>7}  "
              f"{'Gap':>6}  {'Eps/mo':>6}  {'Last':>10}")
    print()
    print(pal.bold(header))
    print(pal.dim("  " + "─" * (len(header) - 2)))
    for report in reports:
        totals, duration, cadence = (report["totals"], report["duration_minutes"],
                                     report["cadence_days"])
        per_month = (f"{totals['episodes_per_month']:.1f}"
                     if totals["episodes_per_month"] else "—")
        median_gap = f"{cadence['median']:.0f}d" if cadence["median"] is not None else "—"
        print(f"  {truncate(report['show']['name'] or '—', name_width):<{name_width}}  "
              f"{totals['episodes_fetched']:>5}  "
              f"{totals['total_hours']:>7.0f}  "
              f"{fmt_minutes(duration['median']):>7}  "
              f"{median_gap:>6}  {per_month:>6}  "
              f"{totals['last_release'] or '—':>10}")
    print()

    if args.json:
        write_json({"generated_at": reports[0]["generated_at"], "shows": reports},
                   Path(args.json))
        print(pal.dim(f"  JSON written to {args.json}"))
    return 0


# ─────────────────────────────────────────────────────────────
#  Watch mode — a live panel beside the Spotify app
# ─────────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).resolve().parent / ".cache"


def _cache_file(show_id: str, market: str) -> Path:
    return CACHE_DIR / f"{show_id}-{market}.json"


def load_cached_catalogue(show_id: str, market: str,
                          max_age_hours: float) -> Optional[Tuple[Dict, List]]:
    """Return a cached (show, episodes) pair, or None if absent or stale.

    The RAW API payloads are cached rather than the finished report: the
    report holds date objects that JSON cannot round-trip, and rebuilding it
    is pure arithmetic that costs nothing next to a 300-episode refetch.
    """
    path = _cache_file(show_id, market)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    age_hours = (time.time() - payload.get("cached_at", 0)) / 3600
    if age_hours > max_age_hours:
        return None
    return payload.get("show") or {}, payload.get("episodes") or []


def save_cached_catalogue(show_id: str, market: str, show: Dict,
                          episodes: List) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(show_id, market).write_text(
            json.dumps({"cached_at": time.time(), "show": show,
                        "episodes": episodes}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written is not worth failing over


def compare_episode(report: Dict[str, Any], episode: Dict[str, Any]) -> Dict[str, Any]:
    """Locate the playing episode inside its own show's distribution."""
    minutes = (episode.get("duration_ms") or 0) / 60000.0
    # Compare against every OTHER episode, then count this one back in. Ranking
    # it against a list that already contains it would let a #61 of 60 through
    # whenever the catalogue copy carries a different duration.
    others = [ep["minutes"] for ep in report["episodes"]
              if ep["duration_ms"] and ep["id"] != episode.get("id")]
    median = report["duration_minutes"]["median"]
    released = parse_release_date(episode.get("release_date") or "",
                                  episode.get("release_date_precision") or "")
    dated = sorted([ep for ep in report["episodes"] if ep["date"]],
                   key=lambda ep: ep["date"])

    gap_before = None
    newer_count = None
    if released:
        earlier = [ep["date"] for ep in dated if ep["date"] < released]
        if earlier:
            gap_before = (released - max(earlier)).days
        newer_count = sum(1 for ep in dated if ep["date"] > released)

    return {
        "minutes": minutes,
        "rank": sum(1 for m in others if m > minutes) + 1,
        "ranked_out_of": len(others) + 1,
        "median_delta": (minutes - median) if median is not None else None,
        "released": released,
        "age_days": (datetime.now(timezone.utc).date() - released).days if released else None,
        "gap_before": gap_before,
        "newer_count": newer_count,
        "in_catalogue": any(ep["id"] == episode.get("id") for ep in report["episodes"]),
    }


def _panel_row(label: str, value: str, width: int) -> str:
    return f"  {label:<14}{clip(value, max(10, width - 16))}"


def render_watch_panel(report: Optional[Dict[str, Any]],
                       comparison: Optional[Dict[str, Any]],
                       now: "nowplaying.NowPlaying", pal: Palette,
                       width: int, status: str = "") -> str:
    """One frame of the live panel. Pure — takes a snapshot, returns text."""
    rule = "─" * width
    lines: List[str] = []
    clock = datetime.now().strftime("%H:%M:%S")
    header = "NOW PLAYING"
    lines.append(pal.bold(header) + pal.dim(clock.rjust(width - len(header))))
    lines.append(pal.dim(rule))

    # -- what the desktop app is doing ------------------------
    if not now.running:
        lines.append("  Spotify is not running.")
        lines.append(pal.dim(clip("  Open it and play a podcast episode.", width)))
    elif now.error:
        lines.append("  " + truncate(now.error, width - 4))
    elif not now.is_podcast:
        lines.append(f"  {pal.dim('Not a podcast —')} {truncate(now.title, width - 20)}")
        if now.artist:
            lines.append(pal.dim(f"  {truncate(now.artist, width - 4)}"))
        lines.append("")
        lines.append(pal.dim(clip("  Play a podcast to see its show's stats.", width)))
    else:
        show = (report or {}).get("show", {})
        totals = (report or {}).get("totals", {})
        lines.append("  " + pal.green(pal.bold(truncate(
            show.get("name") or now.artist or "…", width - 4))))
        if totals:
            lines.append(pal.dim("  " + clip(meta_line(
                show.get("publisher"),
                f"{totals['episodes_fetched']} eps",
                fmt_hours(totals["total_hours"])), width - 4)))
        lines.append("")
        lines.append("  " + truncate(now.title, width - 4))

        # Playback progress, redrawn every poll.
        total_s = now.duration_ms / 1000.0
        track_width = max(10, width - 26)
        filled = int(round((now.position_s / total_s) * track_width)) if total_s else 0
        marks = {"playing": "▶", "paused": "⏸"}.get(now.state, "■")
        lines.append(f"  {pal.green('█' * filled)}{pal.dim('·' * (track_width - filled))} "
                     f"{fmt_clock(now.position_s)} / {fmt_clock(total_s)}  {marks}")

    if status:
        lines.append("")
        lines.append(pal.dim("  " + truncate(status, width - 4)))

    # -- this episode against its own show --------------------
    if report and comparison:
        lines.append("")
        lines.append(pal.bold("THIS EPISODE"))
        lines.append(pal.dim(rule))
        delta = comparison["median_delta"]
        length = fmt_minutes(comparison["minutes"])
        if delta is not None:
            sign = "+" if delta >= 0 else "−"
            length += f"   {sign}{abs(delta):.0f} min vs median"
        lines.append(_panel_row("Length", length, width))
        if comparison["ranked_out_of"]:
            lines.append(_panel_row("Longest rank",
                                    f"#{comparison['rank']} of "
                                    f"{comparison['ranked_out_of']}", width))
        if comparison["released"]:
            age = comparison["age_days"]
            age_text = "today" if age == 0 else f"{age} days ago"
            lines.append(_panel_row("Released",
                                    f"{comparison['released'].isoformat()} · {age_text}",
                                    width))
        if comparison["gap_before"] is not None:
            median_gap = report["cadence_days"]["median"]
            text = f"{comparison['gap_before']} d gap"
            if median_gap is not None:
                text += f"   (show median {median_gap:.0f} d)"
            lines.append(_panel_row("Cadence", text, width))
        if comparison["newer_count"]:
            count = comparison["newer_count"]
            lines.append(_panel_row(
                "Since then",
                f"{count} newer episode{'s' if count > 1 else ''}", width))
        elif comparison["newer_count"] == 0:
            lines.append(_panel_row("Since then", "this is the newest episode", width))
        if not comparison["in_catalogue"]:
            lines.append(pal.dim(clip("  (not in this market's episode list)", width)))

        # -- the show as a whole ------------------------------
        totals = report["totals"]
        lines.append("")
        lines.append(pal.bold("THE SHOW"))
        lines.append(pal.dim(rule))
        lines.append(_panel_row("Rhythm", report["cadence_label"], width))
        lines.append(_panel_row("Median length",
                                fmt_minutes(report["duration_minutes"]["median"]), width))
        lines.append(_panel_row("Last 90 days",
                                f"{report['recent']['last_90_days']} episodes", width))
        if totals["last_release"]:
            since = totals["days_since_last"]
            lines.append(_panel_row("Last drop",
                                    f"{totals['last_release']} · {since} days ago", width))

    lines.append(pal.dim(rule))
    lines.append(pal.dim("  Ctrl-C to quit"))
    return "\n".join(lines)


def _watch_report(client: Spotify, show_id: str, args: argparse.Namespace,
                  cache: Dict[str, Any]) -> Dict[str, Any]:
    """Report for a show, from memory, then disk, then the API."""
    if show_id in cache:
        return cache[show_id]
    catalogue = (None if args.no_cache else
                 load_cached_catalogue(show_id, client.market, args.cache_hours))
    if catalogue is None:
        show = client.show(show_id)
        episodes = client.episodes(show_id, total_hint=show.get("total_episodes"))
        if not args.no_cache:
            save_cached_catalogue(show_id, client.market, show, episodes)
    else:
        show, episodes = catalogue
    cache[show_id] = build_report(show, episodes, client.market)
    return cache[show_id]


def cmd_watch(client: Spotify, args: argparse.Namespace, pal: Palette) -> int:
    """Poll the desktop app and redraw a panel whenever anything changes."""
    client.quiet = True  # progress lines would fight with the panel
    reports: Dict[str, Any] = {}
    episodes: Dict[str, Any] = {}      # episode id -> API episode object
    failures: Dict[str, Tuple[float, str]] = {}
    interactive = sys.stdout.isatty()
    last_frame = ""

    if interactive:
        sys.stdout.write("\033[?25l")   # hide the cursor while the panel owns the screen
    try:
        while True:
            now = nowplaying.read()
            report = comparison = None
            status = ""

            if now.is_podcast:
                episode = episodes.get(now.item_id)
                show_id = (episode or {}).get("show", {}).get("id")
                stale = failures.get(show_id or now.item_id)
                if stale and time.time() - stale[0] < 60:
                    status = stale[1]
                else:
                    try:
                        if episode is None:
                            status = "looking up this episode…"
                            episode = client.get(f"/episodes/{now.item_id}",
                                                 {"market": client.market})
                            episodes[now.item_id] = episode
                        show_id = (episode.get("show") or {}).get("id")
                        if show_id and show_id not in reports:
                            status = f"fetching the episode list for {episode['show'].get('name', '')}…"
                            _render(render_watch_panel(None, None, now, pal,
                                                       _panel_width(), status),
                                    interactive, last_frame)
                        if show_id:
                            report = _watch_report(client, show_id, args, reports)
                            comparison = compare_episode(report, episode)
                            status = ""
                    except SpotifyError as exc:
                        # A single bad lookup must not end the session.
                        failures[show_id or now.item_id] = (time.time(), str(exc))
                        status = str(exc)

            frame = render_watch_panel(report, comparison, now, pal,
                                       _panel_width(), status)
            last_frame = _render(frame, interactive, last_frame)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if interactive:
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()


def _panel_width() -> int:
    import shutil
    return max(46, min(72, shutil.get_terminal_size((60, 24)).columns - 2))


def _render(frame: str, interactive: bool, last_frame: str) -> str:
    """Repaint in place on a terminal; append only on change when piped."""
    if interactive:
        sys.stdout.write("\033[H\033[J" + frame + "\n")
        sys.stdout.flush()
    elif frame != last_frame:
        print(frame, flush=True)
    return frame


# ─────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────

def add_shared_options(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Options accepted both before and after the subcommand.

    Subparser copies use SUPPRESS defaults: without that, an unspecified
    option there would overwrite the value already parsed from the position
    in front of the subcommand.
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--market", default=default(None),
                        help="ISO country code used for catalogue lookups "
                             "(default: SPOTIFY_MARKET, else US). Resolved after "
                             "the .env files are read, not at parse time.")
    parser.add_argument("--client-id", default=default(None),
                        help="overrides SPOTIFY_CLIENT_ID")
    parser.add_argument("--client-secret", default=default(None),
                        help="overrides SPOTIFY_CLIENT_SECRET")
    parser.add_argument("--env-file", default=default(None),
                        help="extra .env file to read")
    parser.add_argument("--max-episodes", type=int, default=default(None),
                        help="stop after N episodes (default: fetch all)")
    parser.add_argument("--no-color", action="store_true", default=default(False),
                        help="plain output")
    parser.add_argument("--quiet", action="store_true", default=default(False),
                        help="suppress progress lines")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-show-stats",
        description="Catalogue statistics for Spotify shows (podcasts). "
                    "`watch` follows the Spotify desktop app live.",
        epilog="Credentials: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, from the "
               "environment or a .env file. Create an app at "
               "https://developer.spotify.com/dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_shared_options(parser)

    # Same flags again for every subcommand, so `stats X --market DE` works
    # exactly like `--market DE stats X`.
    shared = argparse.ArgumentParser(add_help=False)
    add_shared_options(shared, suppress=True)
    sub = parser.add_subparsers(dest="command")

    stats = sub.add_parser("stats", parents=[shared],
                           help="full report for one show (default command)")
    stats.add_argument("show", help="show id, spotify URL/URI, or a name to search")
    stats.add_argument("--top", type=int, default=10,
                       help="how many recent episodes to list (0 = none)")
    stats.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    stats.add_argument("--csv", metavar="PATH", help="write the episode list as CSV")
    stats.add_argument("--html", metavar="PATH", help="write a standalone HTML report")

    search = sub.add_parser("search", parents=[shared], help="find a show's id by name")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)

    watch = sub.add_parser("watch", parents=[shared],
                           help="live panel following the Spotify desktop app")
    watch.add_argument("--interval", type=float, default=5.0,
                       help="seconds between polls (default 5)")
    watch.add_argument("--cache-hours", type=float, default=12.0,
                       help="how long a fetched episode list stays reusable (default 12)")
    watch.add_argument("--no-cache", action="store_true",
                       help="always refetch episode lists")

    compare = sub.add_parser("compare", parents=[shared],
                             help="side-by-side table for several shows")
    compare.add_argument("shows", nargs="+", help="ids, URLs, or names")
    compare.add_argument("--json", metavar="PATH", help="write all reports as JSON")

    return parser


def normalise_argv(argv: List[str]) -> List[str]:
    """Allow `spotify_show_stats.py <show>` as shorthand for `stats <show>`."""
    commands = {"stats", "search", "compare", "watch"}
    # Options whose VALUE is a separate argv entry — that value is not the
    # show name, so stepping over it keeps `--market DE "Some Show"` working.
    takes_value = {"--market", "--client-id", "--client-secret", "--env-file",
                   "--max-episodes"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in commands:
            return argv
        if not token.startswith("-"):
            return argv[:index] + ["stats"] + argv[index:]
        index += 2 if token in takes_value else 1
    return argv


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(normalise_argv(argv))

    if not args.command:
        parser.print_help()
        return 1

    load_env_files(Path(args.env_file) if args.env_file else None)
    pal = Palette(enabled=not args.no_color and sys.stdout.isatty()
                  and not os.environ.get("NO_COLOR"))

    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = args.client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("Missing Spotify credentials.\n\n"
              "  1. Create an app at https://developer.spotify.com/dashboard\n"
              "  2. Copy .env.example to .env next to this script\n"
              "  3. Fill in SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET\n",
              file=sys.stderr)
        return 2

    market = args.market or os.environ.get("SPOTIFY_MARKET") or "US"
    client = Spotify(client_id, client_secret, market=market, quiet=args.quiet)
    handlers = {"stats": cmd_stats, "search": cmd_search, "compare": cmd_compare,
                "watch": cmd_watch}
    try:
        return handlers[args.command](client, args, pal)
    except SpotifyError as exc:
        print(f"\nError: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
