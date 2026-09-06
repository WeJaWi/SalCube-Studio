"""Offline statistics, privacy and measured-ETA regressions."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock

import app
from pipeline import analytics, config, operations, stats
from tests.isolation import isolated_runtime


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_runtime()
        self.root = self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        self.jobs = [
            {"job_id": "a", "profile": "cars-old", "status": "done", "created_at": "2026-09-06T10:00:00Z",
             "performance": {"stages": {"video_render": {"seconds": 120}, "ffmpeg": {"seconds": 240}}}},
            {"job_id": "b", "profile": "classic", "status": "retry_wait", "created_at": "2026-09-05T10:00:00Z"},
            {"job_id": "c", "profile": "classic", "status": "paused", "created_at": "2026-08-01T10:00:00Z"},
            {"job_id": "d", "profile": "classic", "status": "cancelled", "created_at": "2026-09-06T10:00:00Z"},
        ]

    def ledger(self, name, rows):
        (config.OUTPUT_DIR / name).write_text("\n".join(json.dumps(r) for r in rows) + "\ntruncated{")

    def result(self, **kwargs):
        return analytics.snapshot(self.jobs, days=7, now=self.now, **kwargs)

    def test_totals_include_empty_days_and_deduplicate_usage(self):
        self.ledger("ai33_usage.jsonl", [
            {"at": "2026-09-05T12:00:00Z", "intent": "i", "profile": "cars-old", "event": "reserved", "characters": 700},
            {"at": "2026-09-06T12:00:00Z", "intent": "i", "event": "submitted"},
            {"at": "2026-09-06T12:01:00Z", "intent": "i", "event": "succeeded"},
            {"at": "2026-09-06T12:00:00Z", "intent": "pending", "profile": "classic", "event": "reserved", "characters": 50},
            {"at": "2026-09-06T12:00:00Z", "intent": "rejected", "profile": "classic", "event": "rejected", "characters": 99},
        ])
        result = self.result()
        self.assertEqual(result["jobs"]["total"], 2)
        self.assertEqual(result["jobs"]["current"]["paused"], 1)
        self.assertEqual(result["ai33"]["submitted_characters"], 700)
        self.assertEqual(result["ai33"]["daily_average_characters"], 100)
        self.assertEqual(result["ai33"]["pending_characters"], 50)
        self.assertEqual(result["ai33"]["rejected_requests"], 1)
        self.assertEqual(len(result["daily"]), 7)
        self.assertEqual(result["daily"][-2]["characters"], 700)
        self.assertEqual(result["jobs"]["median_render_seconds"], 120)

    def test_kie_terminal_events_keep_original_date_and_credit_amount(self):
        self.ledger("kie_usage.jsonl", [
            {"at": "2026-09-05T12:00:00Z", "task_id": "t", "event": "submitted", "context": "a:beat-1:image"},
            {"at": "2026-09-06T12:00:00Z", "task_id": "t", "event": "succeeded", "credits_consumed": 3},
            {"at": "2026-09-06T12:00:00Z", "task_id": "t", "credits_consumed": None},
            {"at": "2026-09-06T12:00:00Z", "task_id": "unknown", "event": "submitted"},
        ])
        self.assertEqual(self.result()["kie"], {"tasks": 2, "reported_credits": 3.0, "tasks_with_reported_credits": 1})
        self.assertEqual(analytics.kie_tasks()[0]["at"], "2026-09-05T12:00:00Z")
        scoped = self.result(allowed_profiles=["classic"])
        self.assertEqual(scoped["kie"]["tasks"], 0)
        self.assertIsNone(scoped["kie"]["reported_credits"])
        self.assertEqual(scoped["filters"]["profiles"], ["classic"])
        self.assertNotIn("cars-old", json.dumps(scoped))

    def test_invalid_metrics_are_not_counted_as_real_samples(self):
        for stages in ([], ["invalid"], {"video_render": "bad"}, {"video_render": {"seconds": float("nan")}}):
            self.jobs[0]["performance"]["stages"] = stages
            self.assertIsNone(self.result()["jobs"]["median_render_seconds"])
        self.assertIsNone(analytics.date_of("not a date"))
        self.assertEqual(analytics.number(float("inf")), 0)

    def test_authenticated_scoped_api_and_observer_privacy(self):
        access = [{"key": "test-owner", "profiles": None}, {"key": "test-profile", "profiles": ["classic"]}]
        app.JOBS.update({j["job_id"]: j for j in self.jobs})
        with patch.object(app, "ACCESS", access):
            client = app.app.test_client()
            self.assertEqual(client.get("/api/stats").status_code, 401)
            client.set_cookie("studio_key", "test-profile")
            result = client.get("/api/stats?days=365")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            self.assertNotIn("cars-old", result.get_data(as_text=True))
            self.assertEqual(client.get("/api/stats?profile=cars-old").status_code, 403)
            for path in ("/api/ops/status", "/api/ops/report/123", "/api/ops/ai33-key"):
                self.assertEqual(client.get(path).status_code, 403)
            self.assertNotIn('id="statsAsk"', client.get("/stats").get_data(as_text=True))
            client.set_cookie("studio_key", "test-owner")
            self.assertIn('id="statsAsk"', client.get("/stats").get_data(as_text=True))
            for value in ("0", "366", "bad", "1.5"):
                self.assertEqual(client.get("/api/stats?days=" + value).status_code, 400)

    def test_observer_uses_server_aggregates_for_selected_period(self):
        with patch.object(app, "ACCESS", []), patch.object(app, "_ops_center") as center:
            center.return_value.chat.return_value = {"answer": "test"}
            client = app.app.test_client()
            response = client.post("/api/ops/chat", json={"question": "Usage?", "days": 7, "profile": "classic"})
            self.assertEqual(response.status_code, 200)
            snapshot = center.return_value.chat.call_args.kwargs["statistics"]
            self.assertEqual(snapshot["period"]["days"], 7)
            self.assertEqual(snapshot["filters"]["profile"], "classic")
            self.assertEqual(client.post("/api/ops/chat", json={"question": "Usage?", "days": 0}).status_code, 400)

    def test_chat_includes_statistics_and_salcube_identity_without_daily_bulk(self):
        observer = operations.OperationsCenter(lambda: {"jobs": [], "statistics": {"old": True}})
        with patch.object(operations, "_chat_with_fallback", return_value=("answer", "model")) as chat:
            observer.chat("Usage?", statistics={"period": {"days": 7}, "ai33": {"submitted_characters": 700}, "daily": ["unused-daily"]})
            prompt = chat.call_args.args[0]
            self.assertIn('"submitted_characters": 700', prompt)
            self.assertNotIn("unused-daily", prompt)
            self.assertIn("SalCube", chat.call_args.kwargs["system"])

    def test_render_history_is_deduplicated_and_skips_cached_attempts(self):
        perf = {"chars": 1200, "beats": 3, "stages": {"video_render": {"seconds": 100}}}
        for _ in range(3):
            stats.record_performance("classic", "a", perf, "2026-09-06")
        self.assertEqual(len(stats._load()["samples"]["classic"]), 1)
        for counter in ("beat_cache_hits", "final_export_cache_hits"):
            stats.record_performance("classic", counter, {**perf, "counters": {counter: 1}}, "2026-09-06")
        self.assertEqual(len(stats._load()["samples"]["classic"]), 1)
        with patch.object(stats, "record_render", side_effect=OSError("disk")):
            stats.record_performance("classic", "safe", perf, "2026-09-06")
