from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from pipeline import cancel, clients, llm
from tests.isolation import isolated_runtime


def setUpModule():
    isolation = isolated_runtime()
    isolation.__enter__()
    unittest.addModuleCleanup(isolation.__exit__, None, None, None)


class JobStopSafetyTests(unittest.TestCase):
    job_id = "stop-safety-job"

    def setUp(self):
        app.JOBS.clear()
        cancel.set_current(None)
        app.JOBS[self.job_id] = {
            "status": "running", "step": "script", "title": "Test job",
            "profile": "classic", "inputs": {}, "log": [], "retries": 0,
        }

    def tearDown(self):
        cancel.set_current(None)
        app.JOBS.clear()

    def run_job(self, pipeline):
        manager = MagicMock()
        manager.get_log_path.return_value = "isolated-test-log"
        with patch.object(app.jobs.JobManager, "instance", return_value=manager), \
                patch.object(app.orchestrator, "run_pipeline", side_effect=pipeline), \
                patch.object(app.config, "queue_setting", return_value=2), \
                patch.object(app, "_schedule_retry") as retry, \
                patch.object(app, "_upload_job_deliverables") as upload, \
                patch.object(app.traceback, "print_exc"):
            app._run_job(self.job_id, {}, "classic")
        return retry, upload

    def test_stopped_provider_error_preserves_requested_destination(self):
        for target in ("paused", "cancelled", "queued"):
            with self.subTest(target=target):
                self.setUp()

                def provider_error(*args):
                    app.JOBS[self.job_id]["_stop_target"] = target
                    cancel.current().cancel()
                    raise RuntimeError("provider timed out after Stop")

                retry, upload = self.run_job(provider_error)
                job = app.JOBS[self.job_id]
                self.assertEqual(job["status"], target)
                self.assertEqual(job["step"], target)
                self.assertEqual(job["retries"], 0)
                self.assertNotIn("_cancel", job)
                self.assertNotIn("_stop_target", job)
                self.assertNotIn("error", job)
                retry.assert_not_called()
                upload.assert_not_called()

    def test_stop_target_wins_before_token_is_cancelled(self):
        def provider_error(*args):
            # stop_running sets this under LOCK before cancelling the token
            # outside LOCK; the exception can land in that tiny interval.
            app.JOBS[self.job_id]["_stop_target"] = "paused"
            raise RuntimeError("provider error during Stop handoff")

        retry, _ = self.run_job(provider_error)
        self.assertEqual(app.JOBS[self.job_id]["status"], "paused")
        retry.assert_not_called()

    def test_cancelled_pipeline_return_does_not_start_upload(self):
        def pipeline(*args):
            cancel.current().cancel()
            return Path("not-uploaded.mp4")

        retry, upload = self.run_job(pipeline)
        self.assertEqual(app.JOBS[self.job_id]["status"], "paused")
        retry.assert_not_called()
        upload.assert_not_called()

    def test_stop_while_awaiting_approval_cleans_gate_state(self):
        for approval_ready in (False, True):
            with self.subTest(approval_ready=approval_ready):
                self.setUp()
                event = MagicMock()

                def wait(timeout=None):
                    job = app.JOBS[self.job_id]
                    self.assertEqual(job["status"], "awaiting_approval")
                    job["_stop_target"] = "paused"
                    if approval_ready:
                        job["approval"] = {"hook_text": "Approved"}
                    cancel.current().cancel()
                    return approval_ready

                event.wait.side_effect = wait

                def pipeline(*args):
                    gate = args[4]
                    gate({"bold_statement": "Hook"}, "Outline")
                    self.fail("cancelled approval gate returned")

                with patch.object(app.threading, "Event", return_value=event):
                    retry, upload = self.run_job(pipeline)
                job = app.JOBS[self.job_id]
                self.assertEqual(job["status"], "paused")
                for key in ("awaiting", "_gate_event", "approval", "_cancel"):
                    self.assertNotIn(key, job)
                event.wait.assert_called_once_with(timeout=0.5)
                retry.assert_not_called()
                upload.assert_not_called()

    def test_uncertain_submissions_never_schedule_job_retries(self):
        for error_type in (llm.LLMSubmissionUncertain, clients.KieSubmissionUncertain):
            with self.subTest(error_type=error_type.__name__):
                self.setUp()
                retry, upload = self.run_job(error_type("response lost"))
                job = app.JOBS[self.job_id]
                self.assertEqual(job["status"], "error")
                self.assertEqual(job["step"], "error")
                self.assertEqual(job["retries"], 0)
                self.assertIn("automatic retry disabled", job["log"][-1]["message"])
                retry.assert_not_called()
                upload.assert_not_called()

    def test_wrapped_uncertain_submission_is_not_replayed(self):
        def pipeline(*args):
            try:
                raise llm.LLMSubmissionUncertain("response lost")
            except llm.LLMSubmissionUncertain as error:
                raise RuntimeError("script generation failed") from error

        retry, _ = self.run_job(pipeline)
        self.assertEqual(app.JOBS[self.job_id]["status"], "error")
        retry.assert_not_called()

    def test_ordinary_transient_failure_still_retries(self):
        retry, upload = self.run_job(RuntimeError("temporary provider outage"))
        job = app.JOBS[self.job_id]
        self.assertEqual(job["status"], "retry_wait")
        self.assertEqual(job["retries"], 1)
        retry.assert_called_once_with(self.job_id)
        upload.assert_not_called()

    def test_exhausted_retry_budget_stays_failed(self):
        app.JOBS[self.job_id]["retries"] = 2
        retry, _ = self.run_job(RuntimeError("temporary provider outage"))
        self.assertEqual(app.JOBS[self.job_id]["status"], "error")
        self.assertEqual(app.JOBS[self.job_id]["retries"], 2)
        retry.assert_not_called()
