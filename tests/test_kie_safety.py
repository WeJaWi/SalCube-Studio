from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

from pipeline import clients, config, llm, orchestrator


def response(status: int, data: dict) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        headers={},
        text=json.dumps(data),
        json=lambda: data,
    )


class KieClientSafetyTests(unittest.TestCase):
    def setUp(self):
        with clients._KIE_AUDIT_LOCK:
            clients._KIE_TASK_META.clear()
            clients._KIE_TERMINAL_TASKS.clear()
            clients._KIE_TERMINAL_LOG_PATH = ""

    def tearDown(self):
        with clients._KIE_AUDIT_LOCK:
            clients._KIE_TASK_META.clear()
            clients._KIE_TERMINAL_TASKS.clear()
            clients._KIE_TERMINAL_LOG_PATH = ""

    def test_billable_submission_is_not_retried_after_network_failure(self):
        with TemporaryDirectory() as tmp, \
                patch.object(config, "OUTPUT_DIR", Path(tmp)), \
                patch.object(clients.requests, "post",
                             side_effect=requests.ReadTimeout("lost response")) as post:
            with self.assertRaises(clients.KieSubmissionUncertain):
                clients.kie_create_task(
                    "paid-model", {"prompt": "hello"}, api_key="secret-key",
                    context="job-1:beat-1", key_label="video")

            post.assert_called_once()
            rows = [json.loads(line) for line in
                    (Path(tmp) / "kie_usage.jsonl").read_text().splitlines()]

        self.assertEqual(rows[0]["event"], "submission_uncertain")
        self.assertNotIn("secret-key", json.dumps(rows))

    def test_llm_network_failure_is_not_replayed(self):
        spec = {
            "format": "openai", "model": "test-model", "key": "secret",
            "base": "https://kie.invalid", "path": "/chat",
        }
        with patch.dict(config.SCRIPT_MODELS, {"test-kie": spec}), \
                patch.object(config, "CLAUDE_CLI_FALLBACK", False), \
                patch.object(llm.requests, "post",
                             side_effect=requests.ReadTimeout("lost")) as post:
            with self.assertRaises(llm.LLMSubmissionUncertain):
                llm.chat("hello", "test-kie", retries=5,
                         allow_cli_fallback=False)
        post.assert_called_once()

    def test_json_wrapper_does_not_repeat_transport_failures(self):
        with patch.object(llm, "chat",
                          side_effect=RuntimeError("provider unavailable")) as chat:
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                llm.chat_json("hello", "unused", attempts=3,
                              allow_cli_fallback=False)
        chat.assert_called_once()

    def test_success_records_provider_reported_credits(self):
        created = response(200, {
            "code": 200, "data": {"taskId": "kie-task-1"},
        })
        finished = response(200, {
            "code": 200,
            "data": {
                "state": "success",
                "creditsConsumed": 17,
                "resultJson": json.dumps({"resultUrls": ["https://result"]}),
            },
        })
        with TemporaryDirectory() as tmp, \
                patch.object(config, "OUTPUT_DIR", Path(tmp)), \
                patch.object(clients.requests, "post", return_value=created), \
                patch.object(clients.requests, "get", return_value=finished):
            task_id = clients.kie_create_task(
                "paid-model", {"prompt": "hello"}, api_key="secret-key",
                context="job-1:beat-1", key_label="video")
            urls = clients.kie_poll(
                task_id, interval=0, api_key="secret-key", key_label="video")
            # Re-polling a finished task after a download/restart must not make
            # the usage ledger look as though it consumed credits twice.
            clients.kie_poll(
                task_id, interval=0, api_key="secret-key", key_label="video")
            rows = [json.loads(line) for line in
                    (Path(tmp) / "kie_usage.jsonl").read_text().splitlines()]

        self.assertEqual(urls, ["https://result"])
        self.assertEqual([row["event"] for row in rows],
                         ["submitted", "succeeded"])
        self.assertEqual(rows[-1]["credits_consumed"], 17)
        self.assertNotIn("secret-key", json.dumps(rows))


class KieLedgerTests(unittest.TestCase):
    def test_task_id_survives_restart_and_is_cleared_only_after_collection(self):
        with TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({
                "beats": [{"type": "ai_video"}],
                "music_prompt": "music",
                "kie_tasks": {},
            }))
            ledger = orchestrator._KieTaskLedger(plan)
            ledger.begin(0, "ai_video", "video-model")
            ledger.submitted(0, "ai_video", "video-model", "task-123")

            saved = json.loads(plan.read_text())
            resumed = orchestrator._KieTaskLedger(plan, saved["kie_tasks"])
            self.assertEqual(
                resumed.get(0, "ai_video")["task_id"], "task-123")

            resumed.clear(0, "ai_video")
            self.assertEqual(json.loads(plan.read_text())["kie_tasks"], {})

    def test_uncertain_intent_survives_without_a_task_id(self):
        with TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({
                "beats": [{"type": "ai_image"}],
                "music_prompt": "music",
                "kie_tasks": {},
            }))
            ledger = orchestrator._KieTaskLedger(plan)
            ledger.begin(0, "ai_image", "image-model")

            entry = json.loads(plan.read_text())["kie_tasks"]["0:ai_image"]
            self.assertEqual(entry["state"], "submitting")
            self.assertNotIn("task_id", entry)

    def test_fallback_budget_caps_tasks_and_trips_on_credit_errors(self):
        budget = orchestrator._KieFallbackBudget(2)
        self.assertTrue(budget.claim()[0])
        self.assertTrue(budget.claim()[0])
        allowed, reason = budget.claim()
        self.assertFalse(allowed)
        self.assertIn("limit", reason)

        budget = orchestrator._KieFallbackBudget(5)
        budget.note_failure(RuntimeError("kie: insufficient credits"))
        allowed, reason = budget.claim()
        self.assertFalse(allowed)
        self.assertIn("credit", reason)


if __name__ == "__main__":
    unittest.main()
