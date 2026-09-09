"""Offline tests for spotify-show-stats. No credentials or network needed."""
from __future__ import annotations

import json
import os
import random
import sys
import unittest
import unittest.mock
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spotify_show_stats as sss
import nowplaying

SHOW = {
    "id": "4rOoJ6Egrf8K2IrywzwOMk",
    "name": "Testschau",
    "publisher": "Dmiqwi",
    "description": "Ein Test-Podcast.",
    "languages": ["de"],
    "media_type": "audio",
    "explicit": False,
    "total_episodes": 142,
    "external_urls": {"spotify": "https://open.spotify.com/show/x"},
    "images": [{"url": "https://i.scdn.co/x"}],
}


def make_episodes(count: int, step_days: int = 7, seed: int = 7):
    """Newest first, the order Spotify itself returns."""
    random.seed(seed)
    episodes = []
    day = date.today()
    for index in range(count):
        precision = "day" if index % 25 else "month"
        episodes.append({
            "id": f"ep{index}",
            "name": f"Folge {count - index}",
            "release_date": (day.isoformat() if precision == "day"
                             else day.strftime("%Y-%m")),
            "release_date_precision": precision,
            "duration_ms": int(random.gauss(38, 10) * 60000),
            "explicit": index % 17 == 0,
            "language": "de",
            "external_urls": {"spotify": f"https://open.spotify.com/episode/{index}"},
            "description": "Beschreibung.",
        })
        day -= timedelta(days=step_days)
    return episodes


class ShowIdTests(unittest.TestCase):
    def test_accepts_url_uri_and_bare_id(self):
        expected = "4rOoJ6Egrf8K2IrywzwOMk"
        for value in (
            expected,
            "spotify:show:4rOoJ6Egrf8K2IrywzwOMk",
            "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk",
            "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk?si=abc123",
            "https://open.spotify.com/de/show/4rOoJ6Egrf8K2IrywzwOMk",
        ):
            self.assertEqual(sss.extract_show_id(value), expected, value)

    def test_free_text_is_not_mistaken_for_an_id(self):
        self.assertIsNone(sss.extract_show_id("Huberman Lab"))
        self.assertIsNone(sss.extract_show_id("a-name-that-is-22c"))


class ReleaseDateTests(unittest.TestCase):
    def test_only_day_precision_becomes_a_date(self):
        self.assertEqual(sss.parse_release_date("2025-03-04", "day"),
                         date(2025, 3, 4))
        self.assertIsNone(sss.parse_release_date("2025-03", "month"))
        self.assertIsNone(sss.parse_release_date("2025", "year"))
        self.assertIsNone(sss.parse_release_date("", "day"))

    def test_year_survives_lower_precision(self):
        self.assertEqual(sss.parse_year("2025-03"), 2025)
        self.assertIsNone(sss.parse_year(None))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.episodes = make_episodes(120)
        self.report = sss.build_report(SHOW, self.episodes, "DE")

    def test_counts_every_episode_but_dates_only_the_precise_ones(self):
        totals = self.report["totals"]
        self.assertEqual(totals["episodes_fetched"], 120)
        undated = sum(1 for ep in self.episodes
                      if ep["release_date_precision"] != "day")
        self.assertEqual(totals["undated_episodes"], undated)
        self.assertEqual(totals["episodes_dated"], 120 - undated)

    def test_runtime_totals_match_the_raw_durations(self):
        expected_minutes = sum(ep["duration_ms"] for ep in self.episodes) / 60000.0
        self.assertAlmostEqual(self.report["totals"]["total_minutes"],
                               expected_minutes, places=6)
        self.assertAlmostEqual(self.report["totals"]["total_hours"],
                               expected_minutes / 60.0, places=6)

    def test_weekly_releases_read_as_weekly(self):
        report = sss.build_report(SHOW, make_episodes(60, step_days=7), "DE")
        self.assertEqual(report["cadence_days"]["median"], 7.0)
        self.assertIn("weekly", report["cadence_label"])
        self.assertIn("consistent", report["cadence_label"])

    def test_irregular_releases_are_labelled_irregular(self):
        raw = make_episodes(40)
        day = date.today()
        for index, episode in enumerate(raw):
            episode["release_date"] = day.isoformat()
            episode["release_date_precision"] = "day"
            day -= timedelta(days=1 if index % 4 else 140)
        report = sss.build_report(SHOW, raw, "DE")
        self.assertIn("irregular", report["cadence_label"])

    def test_histograms_cover_the_whole_catalogue(self):
        histograms = self.report["histograms"]
        dated = self.report["totals"]["episodes_dated"]
        self.assertEqual(sum(day["count"] for day in histograms["weekday"]), dated)
        self.assertEqual(len(histograms["weekday"]), 7)
        self.assertEqual(len(histograms["monthly"]), 24)
        self.assertEqual(sum(year["count"] for year in histograms["yearly"]),
                         self.report["totals"]["episodes_fetched"])
        self.assertEqual(sum(b["count"] for b in histograms["duration"]),
                         self.report["totals"]["episodes_fetched"])

    def test_extremes_are_ordered(self):
        longest = [ep["minutes"] for ep in self.report["extremes"]["longest"]]
        shortest = [ep["minutes"] for ep in self.report["extremes"]["shortest"]]
        newest = [ep["date"] for ep in self.report["extremes"]["newest"]]
        self.assertEqual(longest, sorted(longest, reverse=True))
        self.assertEqual(shortest, sorted(shortest))
        self.assertEqual(newest, sorted(newest, reverse=True))

    def test_empty_and_single_episode_catalogues_do_not_crash(self):
        for episodes in ([], make_episodes(1)):
            report = sss.build_report(SHOW, episodes, "DE")
            self.assertIsInstance(
                sss.render_terminal(report, sss.Palette(False)), str)
            self.assertIsInstance(sss.render_html(report), str)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.report = sss.build_report(SHOW, make_episodes(30), "DE")

    def test_json_export_is_valid_and_dates_are_serialised(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            sss.write_json(self.report, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["show"]["name"], "Testschau")
            self.assertEqual(len(payload["episodes"]), 30)

    def test_csv_export_has_a_row_per_episode(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.csv"
            sss.write_csv(self.report, path)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 31)  # header + 30 episodes
            self.assertTrue(lines[0].startswith("release_date,name"))

    def test_html_escapes_titles(self):
        raw = make_episodes(2)
        raw[0]["name"] = 'Folge <script>alert("x")</script>'
        page = sss.render_html(sss.build_report(SHOW, raw, "DE"))
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class ArgvTests(unittest.TestCase):
    def test_bare_show_argument_implies_the_stats_command(self):
        self.assertEqual(sss.normalise_argv(["Huberman"]), ["stats", "Huberman"])
        self.assertEqual(sss.normalise_argv(["--no-color", "x"]),
                         ["--no-color", "stats", "x"])

    def test_option_values_are_not_mistaken_for_the_show(self):
        self.assertEqual(sss.normalise_argv(["--market", "DE", "Huberman"]),
                         ["--market", "DE", "stats", "Huberman"])
        self.assertEqual(sss.normalise_argv(["--max-episodes", "50", "Show"]),
                         ["--max-episodes", "50", "stats", "Show"])

    def test_explicit_commands_are_left_alone(self):
        for argv in (["search", "x"], ["compare", "a", "b"],
                     ["stats", "x", "--json", "j"], ["--help"]):
            self.assertEqual(sss.normalise_argv(list(argv)), argv)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200, payload=None, headers=None, reason="OK"):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.reason = reason
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Replays a queued script of responses and records the calls made."""

    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [
            FakeResponse(payload={"access_token": "tok", "expires_in": 3600})])
        self.get_calls = []
        self.post_calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append((url, dict(params or {}), dict(headers or {})))
        return self.get_responses.pop(0)

    def post(self, url, data=None, headers=None, timeout=None):
        self.post_calls += 1
        response = self.post_responses.pop(0)
        if self.post_responses == []:
            self.post_responses = [FakeResponse(
                payload={"access_token": f"tok{self.post_calls}", "expires_in": 3600})]
        return response


def make_client(session):
    client = sss.Spotify("id", "secret", market="DE", quiet=True)
    client._session = session
    return client


class ClientTests(unittest.TestCase):
    def test_token_is_fetched_once_and_reused(self):
        session = FakeSession([FakeResponse(payload={"a": 1}),
                               FakeResponse(payload={"b": 2})])
        client = make_client(session)
        client.get("/shows/x")
        client.get("/shows/y")
        self.assertEqual(session.post_calls, 1)
        self.assertEqual(session.get_calls[0][2]["Authorization"], "Bearer tok")

    def test_bad_credentials_are_reported_clearly(self):
        session = FakeSession([], [FakeResponse(status=400, payload={})])
        with self.assertRaises(sss.SpotifyError) as caught:
            make_client(session).get("/shows/x")
        self.assertIn("client credentials", str(caught.exception))

    def test_rate_limit_is_retried_after_the_stated_delay(self):
        session = FakeSession([
            FakeResponse(status=429, headers={"Retry-After": "1"}),
            FakeResponse(payload={"ok": True}),
        ])
        slept = []
        with unittest.mock.patch.object(sss.time, "sleep", slept.append):
            result = make_client(session).get("/shows/x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(slept, [2.0])

    def test_expired_token_triggers_one_refresh(self):
        session = FakeSession([FakeResponse(status=401, payload={}),
                               FakeResponse(payload={"ok": True})])
        client = make_client(session)
        self.assertEqual(client.get("/shows/x"), {"ok": True})
        self.assertEqual(session.post_calls, 2)

    def test_not_found_mentions_the_market(self):
        session = FakeSession([FakeResponse(status=404, payload={})])
        with self.assertRaises(sss.SpotifyError) as caught:
            make_client(session).get("/shows/x")
        self.assertIn("--market DE", str(caught.exception))

    def test_pagination_collects_pages_and_drops_null_items(self):
        page_one = {"items": [{"id": "a"}, None, {"id": "b"}], "next": "more"}
        page_two = {"items": [{"id": "c"}], "next": None}
        session = FakeSession([FakeResponse(payload=page_one),
                               FakeResponse(payload=page_two)])
        client = make_client(session)
        episodes = client.episodes("show1")
        self.assertEqual([ep["id"] for ep in episodes], ["a", "b", "c"])
        self.assertEqual(session.get_calls[1][1]["offset"], sss.PAGE_SIZE)
        self.assertEqual(session.get_calls[0][1]["market"], "DE")

    def test_max_episodes_stops_early(self):
        page = {"items": [{"id": str(i)} for i in range(50)], "next": "more"}
        session = FakeSession([FakeResponse(payload=page),
                               FakeResponse(payload=page)])
        episodes = make_client(session).episodes("show1", max_episodes=20)
        self.assertEqual(len(episodes), 20)
        self.assertEqual(len(session.get_calls), 1)

    def test_search_drops_null_results(self):
        session = FakeSession([FakeResponse(
            payload={"shows": {"items": [{"id": "a"}, None]}})])
        self.assertEqual(make_client(session).search_shows("x"), [{"id": "a"}])


class CompareEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.raw = make_episodes(60, step_days=7)
        # Give every episode a full date, weekly apart, so cadence is exact.
        for index, episode in enumerate(self.raw):
            episode["release_date_precision"] = "day"
            episode["release_date"] = (date.today() - timedelta(days=7 * index)).isoformat()
        self.report = sss.build_report(SHOW, self.raw, "DE")

    def _episode(self, index, minutes=None):
        episode = dict(self.raw[index])
        if minutes is not None:
            episode["duration_ms"] = int(minutes * 60000)
        episode["show"] = {"id": SHOW["id"], "name": SHOW["name"]}
        return episode

    def test_longest_episode_ranks_first(self):
        result = sss.compare_episode(self.report, self._episode(3, minutes=600))
        self.assertEqual(result["rank"], 1)
        self.assertEqual(result["ranked_out_of"], 60)
        self.assertGreater(result["median_delta"], 0)

    def test_shortest_episode_ranks_last(self):
        result = sss.compare_episode(self.report, self._episode(3, minutes=1))
        self.assertEqual(result["rank"], 60)
        self.assertLess(result["median_delta"], 0)

    def test_gap_and_position_are_measured_against_the_catalogue(self):
        result = sss.compare_episode(self.report, self._episode(3))
        self.assertEqual(result["gap_before"], 7)
        self.assertEqual(result["newer_count"], 3)
        self.assertTrue(result["in_catalogue"])

    def test_newest_episode_has_nothing_newer(self):
        self.assertEqual(sss.compare_episode(self.report, self._episode(0))["newer_count"], 0)

    def test_episode_absent_from_the_market_list_is_flagged(self):
        stranger = self._episode(0)
        stranger["id"] = "not-in-this-market"
        result = sss.compare_episode(self.report, stranger)
        self.assertFalse(result["in_catalogue"])


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.raw = make_episodes(40)
        self.report = sss.build_report(SHOW, self.raw, "DE")
        episode = dict(self.raw[1])
        episode["show"] = {"id": SHOW["id"], "name": SHOW["name"]}
        self.comparison = sss.compare_episode(self.report, episode)
        self.playing = nowplaying.NowPlaying(
            running=True, state="playing", kind="episode", item_id=episode["id"],
            title=episode["name"], artist=SHOW["name"],
            duration_ms=episode["duration_ms"], position_s=600)

    def _lines(self, *args, **kwargs):
        return sss.render_watch_panel(*args, **kwargs).splitlines()

    def test_every_state_renders_inside_the_given_width(self):
        pal = sss.Palette(False)
        states = [
            (self.report, self.comparison, self.playing, ""),
            (None, None, nowplaying.NowPlaying(running=False), ""),
            (None, None, nowplaying.NowPlaying(running=True, error="denied"), ""),
            (None, None, nowplaying.NowPlaying(running=True, kind="track",
                                               title="A Song", artist="A Band",
                                               duration_ms=1000), ""),
            (None, None, self.playing, "fetching…"),
        ]
        for width in (46, 56, 72):
            for report, comparison, now, status in states:
                for line in self._lines(report, comparison, now, pal, width, status):
                    self.assertLessEqual(len(line), width, line)

    def test_panel_reports_the_show_and_the_episode(self):
        text = sss.render_watch_panel(self.report, self.comparison, self.playing,
                                      sss.Palette(False), 60)
        self.assertIn("Testschau", text)
        self.assertIn("THIS EPISODE", text)
        self.assertIn("Rhythm", text)

    def test_music_panel_makes_no_claims_about_shows(self):
        music = nowplaying.NowPlaying(running=True, kind="track", title="A Song",
                                      artist="A Band", duration_ms=1000)
        text = sss.render_watch_panel(None, None, music, sss.Palette(False), 56)
        self.assertIn("Not a podcast", text)
        self.assertNotIn("THIS EPISODE", text)


class CacheTests(unittest.TestCase):
    def test_catalogue_survives_a_round_trip_and_expires(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(sss, "CACHE_DIR", Path(tmp)):
                episodes = make_episodes(5)
                sss.save_cached_catalogue("show1", "DE", SHOW, episodes)
                cached = sss.load_cached_catalogue("show1", "DE", 12)
                self.assertIsNotNone(cached)
                self.assertEqual(cached[0]["name"], "Testschau")
                self.assertEqual(len(cached[1]), 5)
                # A rebuilt report must match one built from the live payload.
                self.assertEqual(
                    sss.build_report(*cached, "DE")["totals"],
                    sss.build_report(SHOW, episodes, "DE")["totals"])
                self.assertIsNone(sss.load_cached_catalogue("show1", "DE", 0))
                self.assertIsNone(sss.load_cached_catalogue("missing", "DE", 12))


class FakeClient:
    """Stands in for Spotify inside the watch loop."""

    def __init__(self, fail_on_episode=False):
        self.market = "DE"
        self.quiet = False
        self.episode_calls = 0
        self.show_calls = 0
        self.list_calls = 0
        self.fail_on_episode = fail_on_episode

    def get(self, path, params=None):
        self.episode_calls += 1
        if self.fail_on_episode:
            raise sss.SpotifyError("episode is not available in DE")
        return {"id": path.rsplit("/", 1)[-1], "name": "Folge 1",
                "release_date": date.today().isoformat(),
                "release_date_precision": "day", "duration_ms": 2400000,
                "show": {"id": SHOW["id"], "name": SHOW["name"]}}

    def show(self, show_id):
        self.show_calls += 1
        return SHOW

    def episodes(self, show_id, max_episodes=None, total_hint=None):
        self.list_calls += 1
        return make_episodes(30)


class WatchLoopTests(unittest.TestCase):
    def _run(self, client, polls, iterations=3):
        """Drive cmd_watch for N polls, then interrupt it like Ctrl-C would."""
        args = SimpleNamespace(interval=0, no_cache=True, cache_hours=12)
        ticks = {"n": 0}

        def fake_sleep(_seconds):
            ticks["n"] += 1
            if ticks["n"] >= iterations:
                raise KeyboardInterrupt

        script = list(polls)

        def fake_read(timeout=5):
            return script[min(ticks["n"], len(script) - 1)]

        buffer = StringIO()
        with unittest.mock.patch.object(sss.nowplaying, "read", fake_read), \
             unittest.mock.patch.object(sss.time, "sleep", fake_sleep), \
             redirect_stdout(buffer):
            code = sss.cmd_watch(client, args, sss.Palette(False))
        return code, buffer.getvalue()

    @staticmethod
    def _episode_playing(item_id="ep-1"):
        return nowplaying.NowPlaying(running=True, state="playing", kind="episode",
                                     item_id=item_id, title="Folge 1",
                                     artist="Testschau", duration_ms=2400000,
                                     position_s=120)

    def test_one_episode_is_fetched_once_then_served_from_memory(self):
        client = FakeClient()
        code, output = self._run(client, [self._episode_playing()], iterations=4)
        self.assertEqual(code, 0)
        self.assertEqual(client.episode_calls, 1)
        self.assertEqual(client.show_calls, 1)
        self.assertEqual(client.list_calls, 1)
        self.assertIn("Testschau", output)

    def test_music_makes_no_api_calls(self):
        client = FakeClient()
        music = nowplaying.NowPlaying(running=True, state="playing", kind="track",
                                      item_id="t1", title="A Song", artist="A Band",
                                      duration_ms=180000, position_s=5)
        _, output = self._run(client, [music])
        self.assertEqual((client.episode_calls, client.show_calls), (0, 0))
        self.assertIn("Not a podcast", output)

    def test_closed_spotify_makes_no_api_calls(self):
        client = FakeClient()
        _, output = self._run(client, [nowplaying.NowPlaying(running=False)])
        self.assertEqual(client.episode_calls, 0)
        self.assertIn("not running", output)

    def test_a_failed_lookup_is_shown_and_not_retried_immediately(self):
        client = FakeClient(fail_on_episode=True)
        code, output = self._run(client, [self._episode_playing()], iterations=5)
        self.assertEqual(code, 0)
        self.assertEqual(client.episode_calls, 1)   # the failure is remembered
        self.assertIn("not available in DE", output)


class OptionPlacementTests(unittest.TestCase):
    """Shared flags must work on either side of the subcommand."""

    def setUp(self):
        self.parser = sss.build_parser()

    def test_market_is_accepted_before_or_after_the_command(self):
        self.assertEqual(self.parser.parse_args(["--market", "DE", "stats", "X"]).market, "DE")
        self.assertEqual(self.parser.parse_args(["stats", "X", "--market", "NL"]).market, "NL")

    def test_a_value_given_early_is_not_wiped_by_the_subparser(self):
        # The subparser copies use SUPPRESS defaults precisely so this holds.
        args = self.parser.parse_args(["--market", "DE", "--quiet", "stats", "X"])
        self.assertEqual(args.market, "DE")
        self.assertTrue(args.quiet)

    def test_max_episodes_after_the_show_name(self):
        args = self.parser.parse_args(["stats", "X", "--max-episodes", "60"])
        self.assertEqual(args.max_episodes, 60)

    def test_every_subcommand_accepts_the_shared_flags(self):
        for argv in (["stats", "X", "--market", "FR"], ["search", "q", "--market", "FR"],
                     ["watch", "--market", "FR"], ["compare", "a", "b", "--market", "FR"]):
            self.assertEqual(self.parser.parse_args(argv).market, "FR", argv)


class MarketResolutionTests(unittest.TestCase):
    """--market has no parse-time default: .env is not loaded that early."""

    def _market_used(self, argv, env):
        captured = {}

        class FakeSpotify:
            def __init__(self, client_id, client_secret, market="US",
                         timeout=30, quiet=False):
                captured["market"] = market
                self.market = market

        environment = {"SPOTIFY_CLIENT_ID": "a", "SPOTIFY_CLIENT_SECRET": "b"}
        environment.update(env)
        with unittest.mock.patch.object(sss, "load_env_files", lambda *a, **k: None), \
             unittest.mock.patch.object(sss, "Spotify", FakeSpotify), \
             unittest.mock.patch.object(sss, "cmd_search", lambda c, a, p: 0), \
             unittest.mock.patch.dict(os.environ, environment, clear=True):
            code = sss.main(argv)
        return code, captured["market"]

    def test_environment_market_is_honoured(self):
        self.assertEqual(self._market_used(["search", "x"], {"SPOTIFY_MARKET": "NL"}),
                         (0, "NL"))

    def test_us_is_the_last_resort(self):
        self.assertEqual(self._market_used(["search", "x"], {}), (0, "US"))

    def test_the_flag_beats_the_environment(self):
        self.assertEqual(
            self._market_used(["--market", "FR", "search", "x"], {"SPOTIFY_MARKET": "NL"}),
            (0, "FR"))
        self.assertEqual(
            self._market_used(["search", "x", "--market", "FR"], {"SPOTIFY_MARKET": "NL"}),
            (0, "FR"))


class MetaLineTests(unittest.TestCase):
    def test_missing_pieces_leave_no_stray_separators(self):
        # Spotify stopped returning `publisher`; None must simply vanish.
        self.assertEqual(sss.meta_line(None, "251 episodes", "en"), "251 episodes  ·  en")
        self.assertEqual(sss.meta_line(None, "", None), "")
        self.assertEqual(sss.meta_line("Acme", "en"), "Acme  ·  en")


class FormattingTests(unittest.TestCase):
    def test_minutes_become_hours_past_the_hour(self):
        self.assertEqual(sss.fmt_minutes(42.4), "42 min")
        self.assertEqual(sss.fmt_minutes(90), "1h 30m")
        self.assertEqual(sss.fmt_minutes(None), "—")

    def test_bars_stay_within_their_width(self):
        self.assertEqual(len(sss.bar(3, 10, width=20)), 20)
        self.assertEqual(sss.bar(10, 10, width=8), "█" * 8)
        self.assertEqual(sss.bar(5, 0), "")

    def test_sparkline_has_one_column_per_value(self):
        self.assertEqual(len(sss.sparkline([0, 1, 5, 2])), 4)
        self.assertEqual(sss.sparkline([0, 0, 0]), "   ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
