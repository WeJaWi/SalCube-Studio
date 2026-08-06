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
