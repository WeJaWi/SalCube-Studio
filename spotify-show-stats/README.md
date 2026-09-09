# spotify-show-stats

Statistics for Spotify **shows** (podcasts), from the terminal.

Two ways to use it:

- **`watch`** — a live panel that runs beside the Spotify desktop app. It
  notices whatever podcast you are playing and shows that show's stats,
  plus how the current episode compares to the rest of it.
- **`stats`** — a one-off report for any show you name.

Both read the public Spotify Web API and report what the catalogue actually
looks like: how long episodes run, how often they land, on which weekdays,
how the output has changed year over year, and where the gaps are.

## Watch mode

```bash
python3 spotify_show_stats.py watch
```

Park it in a terminal window next to Spotify. It polls the desktop app every
five seconds and redraws:

```
NOW PLAYING                                       18:14:49
──────────────────────────────────────────────────────────
  Huberman Lab
  312 eps  ·  640 h (26.7 days)

  Essentials: How to Focus and Sharpen Attention
  ████████████·················· 38:12 / 1:36:00  ▶

THIS EPISODE
──────────────────────────────────────────────────────────
  Length        1h 36m   +32 min vs median
  Longest rank  #14 of 312
  Released      2026-08-06 · 14 days ago
  Cadence       7 d gap   (show median 7 d)
  Since then    2 newer episodes

THE SHOW
──────────────────────────────────────────────────────────
  Rhythm        roughly weekly, very consistent
  Median length 64 min
  Last 90 days  13 episodes
  Last drop     2026-08-13 · 7 days ago
──────────────────────────────────────────────────────────
  Ctrl-C to quit
```

How it reads your player: the macOS Spotify app answers AppleScript queries
locally, so no login, no OAuth, and nothing about your account leaves the
machine — only the episode id is looked up against the public API. **The
first run raises a macOS permission prompt** ("Terminal wants to control
Spotify"); approve it, or later under System Settings → Privacy & Security →
Automation.

The watcher never launches Spotify. If Spotify is closed, or you are playing
music rather than a podcast, the panel says so and keeps polling.

Each show's episode list is cached on disk for 12 hours (`.cache/`), so
returning to a show you have already opened is instant. `--no-cache` always
refetches; `--cache-hours N` changes the window; `--interval N` changes the
poll rate.

Watch mode reads the desktop app only — episodes playing on your phone or in
the web player are not visible to it.

## One-off reports

Point it at a show — by name, id, or URL — and it pulls the complete episode
list and reports the same statistics in full.

```
$ python3 spotify_show_stats.py "Matthias Richter"
```

```
CATALOGUE
──────────────────────────────────────────────────────────────────────────
  Episodes analysed      140   (Spotify lists 142 for this market)
  Total runtime          87 h (3.6 days)
  First / last release   2023-01-15  →  2026-07-30
  Active span            1292 days (3.5 years)
  Days since last drop   21

EPISODE LENGTH
──────────────────────────────────────────────────────────────────────────
  Median                 37 min
  Shortest / longest     3 min  →  1h 15m

  30-45 min     ████████████████████████████ 59
  45-60 min     █████████████████··········· 36

RELEASE CADENCE
──────────────────────────────────────────────────────────────────────────
  Rhythm                 roughly weekly, irregular
  Median gap             7.0 d
  Episodes per month     3.2 (lifetime avg)
```

## What this can and cannot show

**It can** report everything in Spotify's public catalogue: every episode with
its title, release date, runtime, language and explicit flag, plus every
statistic derived from those.

**It cannot** report plays, streams, listeners, followers, completion rate,
retention or demographics. Spotify does not expose audience metrics through
any public API — those live only in
[Spotify for Creators](https://creators.spotify.com), visible to the owner of
the show, and are exported by hand from that dashboard. No third-party tool
can read them without your account credentials, whatever it may claim.

The upside of working from public catalogue data: it works for **any** show,
including your competitors', not just your own.

## Setup

1. Create an app at <https://developer.spotify.com/dashboard>. No redirect URI
   is needed — this tool uses the app-only client-credentials flow, so it
   never logs into a personal Spotify account.
2. `cp .env.example .env` and paste in the client id and secret. (They may
   also go in the studio's root `.env`; both files are read.)
3. `pip3 install -r requirements.txt` — only `requests`, which the studio
   already has.

## Usage

```bash
# Full report — accepts a name, an id, or any Spotify URL/URI
python3 spotify_show_stats.py "Huberman Lab"
python3 spotify_show_stats.py https://open.spotify.com/show/79CkJF3UJTHFV8Dse3Oy0P

# Find a show's id
python3 spotify_show_stats.py search "psychologie"

# Several shows side by side
python3 spotify_show_stats.py compare "Huberman Lab" "Lex Fridman" 4rOoJ6Egrf8K2IrywzwOMk

# Export alongside the terminal report
python3 spotify_show_stats.py "Huberman Lab" \
    --json report.json --csv episodes.csv --html report.html
```

### Options

| Flag | Effect |
|------|--------|
| `--market XX` | Catalogue market, ISO country code (default `US`, or `SPOTIFY_MARKET`). A show absent in one market may exist in another. |
| `--max-episodes N` | Stop after N episodes instead of fetching the whole back catalogue. |
| `--top N` | How many recent episodes to list (`0` for none). |
| `--json / --csv / --html PATH` | Write the full report, the episode table, or a standalone HTML page. |
| `--client-id / --client-secret` | Override the `.env` values. |
| `--env-file PATH` | Read an additional `.env`. |
| `--no-color`, `--quiet` | Plain output; suppress progress lines. |
| `--interval N` | *(watch)* Seconds between polls, default 5. |
| `--cache-hours N`, `--no-cache` | *(watch)* Episode-list cache window, default 12 h. |

`stats` is the default subcommand, so the show argument can be given on its
own.

## Notes on the numbers

- **Undated episodes.** Spotify records some older releases with only
  month or year precision. Assuming "the 1st" would invent a cadence that
  never happened, so those episodes count toward totals and runtime but are
  left out of gap, weekday and monthly statistics. The report says how many
  were set aside.
- **Episode count mismatch.** When the analysed count differs from the total
  Spotify reports, the difference is region-locked or withdrawn episodes;
  the report shows both numbers.
- **Cadence label** combines the median gap (the typical rhythm) with the
  spread of gaps around it (how reliably that rhythm is kept).
- **Rate limits** are handled automatically — the client honours
  `Retry-After` and retries on 5xx and expired tokens.
- **No publisher.** Spotify's API no longer returns a `publisher` field on
  shows (and `copyrights` comes back empty), so metadata lines omit it rather
  than printing a placeholder. The code still reads the field, so shows would
  show one again if Spotify restores it.
- **Market.** `--market` is resolved *after* the `.env` files are read, so
  `SPOTIFY_MARKET` in `.env` applies. An explicit `--market` always wins, and
  works either before or after the subcommand, as do all the shared flags.

## Tests

`python3 test_spotify_show_stats.py` — 49 tests, all offline: no credentials,
no network, and no Spotify app required. They cover the analysis, all three
renderers, the exports, the disk cache, the HTTP client's retry and
pagination behaviour (against a mocked transport), the watch loop's state
machine, and option placement — including empty and single-episode
catalogues.
