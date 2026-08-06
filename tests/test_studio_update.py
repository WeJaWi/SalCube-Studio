from __future__ import annotations

import unittest
from unittest.mock import patch

import app
from pipeline import identity, llm, operations


class IdentityTests(unittest.TestCase):
    def test_numeric_ids_are_unique_twenty_digit_strings(self):
        values = {identity.numeric_id() for _ in range(200)}
        self.assertEqual(len(values), 200)
        self.assertTrue(all(identity.is_numeric_id(v) for v in values))


class VideoGroupTests(unittest.TestCase):
    def test_small_group_is_inside_requested_range(self):
        for _ in range(100):
            inputs = app._build_inputs({"video_group": "small"}, "A title")
            self.assertGreaterEqual(inputs["length_chars"], 32000)
            self.assertLessEqual(inputs["length_chars"], 47000)
            self.assertEqual(inputs["video_group"], "small")

    def test_big_group_is_inside_requested_range(self):
        for _ in range(100):
            inputs = app._build_inputs({"video_group": "big"}, "A title")
            self.assertGreaterEqual(inputs["length_chars"], 180000)
            self.assertLessEqual(inputs["length_chars"], 210000)
            self.assertEqual(inputs["video_group"], "big")


class OperationsTests(unittest.TestCase):
    def test_monitor_falls_back_from_openrouter_to_terra(self):
        with patch.object(operations.llm, "chat_json", side_effect=[
                RuntimeError("openrouter unavailable"), {"severity": "ok"}]) as mocked:
            result, model = operations._chat_json_with_fallback(
                "inspect", max_tokens=650)
        self.assertEqual(result, {"severity": "ok"})
        self.assertEqual(model, "gpt-5.6-terra")
        self.assertEqual(mocked.call_args_list[0].args[1], "openrouter-monitor")
        self.assertEqual(mocked.call_args_list[1].args[1], "gpt-5.6-terra")
        self.assertFalse(mocked.call_args_list[0].kwargs["allow_cli_fallback"])
        self.assertFalse(mocked.call_args_list[1].kwargs["allow_cli_fallback"])

    def test_monitor_uses_openrouter_without_calling_terra_when_healthy(self):
        with patch.object(operations.llm, "chat",
                          return_value="healthy") as mocked:
            result, model = operations._chat_with_fallback("inspect")
        self.assertEqual(result, "healthy")
        self.assertEqual(model, "qwen/qwen3.7-flash")
        mocked.assert_called_once()

    def test_openrouter_monitor_payload_uses_qwen_reasoning(self):
        spec = {
            "format": "openai", "model": "qwen/qwen3.7-flash",
            "reasoning": True, "reasoning_max_tokens": 256,
        }
        payload = llm._payload(spec, "inspect this", 650)
        self.assertEqual(payload["model"], "qwen/qwen3.7-flash")
        self.assertEqual(payload["reasoning"], {
            "enabled": True, "max_tokens": 256, "exclude": True,
        })
        self.assertEqual(payload["messages"][0]["content"], "inspect this")

    def test_openrouter_monitor_response_parser(self):
        spec = {"format": "openai"}
        result = llm._parse(spec, {
            "choices": [{"message": {"content": "System healthy"}}],
        })
        self.assertEqual(result, "System healthy")

    def test_deterministic_health_escalates_stalled_work(self):
        result = operations.OperationsCenter._deterministic_health({
            "jobs": [{"status": "running", "stalled": True}],
            "workers": [], "disk": {"free_gb": 100, "free_percent": 50},
        })
        self.assertEqual(result["severity"], "critical")
        self.assertTrue(result["issues"])

    def test_deterministic_health_is_ok_when_clear(self):
        result = operations.OperationsCenter._deterministic_health({
            "jobs": [], "workers": [],
            "disk": {"free_gb": 100, "free_percent": 50},
        })
        self.assertEqual(result, {"severity": "ok", "issues": []})

    def test_healthy_disk_is_not_misclassified_by_percentage_alone(self):
        result = operations.OperationsCenter._deterministic_health({
            "jobs": [], "workers": [],
            "disk": {"free_gb": 137.7, "free_percent": 27.8},
        })
        self.assertEqual(result, {"severity": "ok", "issues": []})

    def test_drive_consent_is_warning_but_not_root_cause_report(self):
        snapshot = {
            "jobs": [], "workers": [],
            "disk": {"free_gb": 137.7, "free_percent": 27.8},
            "drive": {"configured": True, "authorized": False},
        }
        health = operations.OperationsCenter._deterministic_health(snapshot)
        triggers = operations.OperationsCenter._automatic_report_triggers(snapshot)
        self.assertEqual(health["severity"], "warning")
        self.assertIn("authorization", health["issues"][0])
        self.assertEqual(triggers, [])

    def test_report_trigger_is_stable_for_real_job_error(self):
        snapshot = {
            "jobs": [{"job_id": "job-1", "status": "error"}],
            "workers": [], "disk": {"free_gb": 100, "free_percent": 50},
        }
        self.assertEqual(
            operations.OperationsCenter._automatic_report_triggers(snapshot),
            ["job_errors:job-1"])


class ThumbnailApprovalTests(unittest.TestCase):
    def setUp(self):
        self.old_access = app.ACCESS
        app.ACCESS = []
        app.JOBS.clear()
        self.job_id = "12345678901234567890"
        app.JOBS[self.job_id] = {
            "status": "done", "step": "done", "title": "A title",
            "profile": "classic", "inputs": {}, "log": [],
            "thumbnail": {"state": "words_ready"},
        }
        self.client = app.app.test_client()

    def tearDown(self):
        app.JOBS.clear()
        app.ACCESS = self.old_access

    def test_words_approval_starts_independent_image_state(self):
        with patch.object(app, "_persist_queue"), patch.object(app.threading, "Thread"):
            response = self.client.post(
                f"/api/thumbnail/job/{self.job_id}/words/approve",
                json={"main_text": "Change Your Mind", "red_word": "Change",
                      "yellow_word": "Mind", "name": "Joseph Murphy"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.job_id]["status"], "done")
        self.assertEqual(app.JOBS[self.job_id]["thumbnail"]["state"],
                         "generating_image")

    def test_image_approval_does_not_change_render_status(self):
        app.JOBS[self.job_id]["thumbnail"] = {
            "state": "image_ready", "thumbnail_id": "09876543210987654321",
            "url": "/api/thumbnail/file/09876543210987654321"}
        with patch.object(app, "_persist_queue"):
            response = self.client.post(
                f"/api/thumbnail/job/{self.job_id}/image/approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.job_id]["status"], "done")
        self.assertEqual(app.JOBS[self.job_id]["thumbnail"]["state"], "approved")


if __name__ == "__main__":
    unittest.main()
