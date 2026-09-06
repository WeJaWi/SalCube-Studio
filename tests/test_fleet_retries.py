"""Fleet retries retain their owner, profile snapshot and capacity reservation."""
import time
import unittest
from unittest.mock import MagicMock, patch

import app
from pipeline import cancel, config, fleet, llm
from tests.isolation import isolated_runtime


class FleetRetryTests(unittest.TestCase):
    def setUp(self):
        context = isolated_runtime()
        self.root = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.addCleanup(cancel.set_current, None)
        self.access = patch.object(app, "ACCESS", [])
        self.access.start()
        self.addCleanup(self.access.stop)
        self.client = app.app.test_client()
        self.jid = "12345678901234567890"
        self.reporter = fleet.FleetReporter(self.jid, "attempt-one")
        self.spec = {"job_id": self.jid, "attempt_id": "attempt-one",
                     "profile": "cars-old", "inputs": {"title": "Classic car"},
                     "profile_data": {"voices": {"English": "elevenlabs_cIOaiiemAqOmrhA4l3E5"}}}

    def waiting(self):
        at = time.time() + 60
        self.reporter.retry_wait("temporary failure", 1, 2, at)
        app.JOBS[self.jid] = {"status": "retry_wait", "step": "retry_wait",
                              "profile": "cars-old", "inputs": {}, "log": [],
                              "retry_at": at, "_fleet_reporter": self.reporter}

    def test_waiting_retry_reserves_worker_and_local_scheduler_slots(self):
        self.waiting()
        worker = fleet.FleetWorker(lambda *_: self.fail("new job was started"))
        worker._jobs[self.jid] = self.reporter
        with patch.object(config, "FLEET_MAX_JOBS", 1), patch.object(config, "gpu_encoder", return_value="h264_nvenc"):
            payload = worker._payload()
            worker._handle({"dispatch": [{"job_id": "different-job", "attempt_id": "new"}]})
        self.assertEqual(payload["capacity"]["active"], 1)
        self.assertEqual(payload["capacity"]["free"], 0)
        self.assertEqual(app._active_slot_count(), 1)
        self.assertNotIn("different-job", worker._jobs)
        worker._handle({"acknowledged_attempts": [{"job_id": self.jid, "attempt_id": "attempt-one"}]})
        self.assertIn(self.jid, worker._jobs)

    def test_retry_uses_same_runner_reporter_and_voice_snapshot(self):
        seen = []
        def run(jid, inputs, profile, *, profile_data, fleet_reporter):
            seen.append((jid, fleet_reporter, profile_data))
            if len(seen) == 1:
                app.JOBS[jid].update(status="retry_wait", step="retry_wait", retry_at=time.time() - 1)
                fleet_reporter.retry_wait("temporary", 1, 2, time.time() - 1)
            else:
                self.assertEqual(fleet_reporter.snapshot()["status"], "running")
                app.JOBS[jid]["status"] = "done"
                fleet_reporter.complete({})
        with patch.object(app, "_run_job", side_effect=run), patch.object(app, "_kick_queue"):
            app._run_fleet_worker_job(self.spec, self.reporter)
        self.assertEqual(len(seen), 2)
        for jid, reporter, snapshot in seen:
            self.assertEqual(jid, self.jid)
            self.assertIs(reporter, self.reporter)
            self.assertIs(snapshot, self.spec["profile_data"])
        self.assertEqual(self.reporter.snapshot()["status"], "done")

    def test_worker_exception_publishes_retry_without_terminal_error_or_local_timer(self):
        app.JOBS[self.jid] = {"status": "running", "profile": "cars-old", "log": [], "inputs": {}, "_fleet_reporter": self.reporter}
        manager = MagicMock()
        with patch.object(app.jobs.JobManager, "instance", return_value=manager), \
                patch.object(app.orchestrator, "run_pipeline", side_effect=RuntimeError("temporary")), \
                patch.object(config, "queue_setting", return_value=2), \
                patch.object(app, "_schedule_retry") as schedule, \
                patch.object(app.traceback, "print_exc"):
            app._run_job(self.jid, {}, "cars-old", fleet_reporter=self.reporter)
        self.assertEqual(app.JOBS[self.jid]["status"], "retry_wait")
        self.assertEqual(self.reporter.snapshot()["status"], "retry_wait")
        self.assertEqual(self.reporter.snapshot()["retries"], 1)
        schedule.assert_not_called()

    def test_final_or_uncertain_failure_does_not_retry(self):
        for error, retries in [(RuntimeError("temporary"), 2), (llm.LLMSubmissionUncertain("lost"), 0)]:
            app.JOBS[self.jid] = {"status": "running", "profile": "cars-old", "log": [], "inputs": {}, "retries": retries}
            with patch.object(app.jobs.JobManager, "instance", return_value=MagicMock()), \
                    patch.object(app.orchestrator, "run_pipeline", side_effect=error), \
                    patch.object(config, "queue_setting", return_value=2), \
                    patch.object(app, "_schedule_retry") as schedule, \
                    patch.object(app.traceback, "print_exc"):
                app._run_job(self.jid, {}, "cars-old", fleet_reporter=self.reporter)
            self.assertEqual(self.reporter.snapshot()["status"], "error")
            schedule.assert_not_called()

    def test_stop_during_retry_wait_prevents_next_attempt(self):
        self.waiting()
        app._abort_local_job(self.jid)
        self.assertFalse(app._wait_for_fleet_retry(self.jid, self.reporter))
        self.assertEqual(app.JOBS[self.jid]["status"], "cancelled")
        self.assertEqual(self.reporter.snapshot()["status"], "error")

    def test_worker_ui_stop_during_retry_wait_releases_reporter(self):
        self.waiting()
        response = self.client.post(f"/api/queue/{self.jid}/stop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.jid]["status"], "paused")
        self.assertFalse(app._wait_for_fleet_retry(self.jid, self.reporter))
        self.assertEqual(self.reporter.snapshot()["status"], "error")

    def test_main_accepts_retry_state_and_does_not_dispatch_into_reserved_slot(self):
        app.JOBS[self.jid] = {"status": "running", "profile": "cars-old", "log": [],
                              "dispatched_to": "windows", "attempt_id": "attempt-one"}
        app.JOBS["next-job"] = {"status": "queued", "profile": "cars-old", "log": [], "inputs": {}}
        self.reporter.retry_wait("temporary", 1, 2, time.time() + 60)
        with patch.object(config, "FLEET_ROLE", "main"), patch.object(config, "FLEET_PAIRING_TOKEN", "test-token"):
            response = self.client.post("/api/fleet/heartbeat", headers={"Authorization": "Bearer test-token"}, json={
                "worker_id": "windows", "protocol_version": 4,
                # Even a buggy free count cannot override the reservation.
                "capacity": {"max_jobs": 1, "active": 0, "free": 1},
                "jobs": [self.reporter.snapshot()]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.jid]["status"], "retry_wait")
        self.assertEqual(app.JOBS[self.jid]["retry_at"], self.reporter.snapshot()["retry_at"])
        self.assertEqual(response.get_json()["dispatch"], [])
        self.assertEqual(response.get_json()["acknowledged_attempts"], [])
        self.assertEqual(app._fleet_capacity(app.FLEET_WORKERS["windows"])["free"], 0)

    def test_main_stop_of_remote_waiting_retry_sends_abort_before_releasing(self):
        app.JOBS[self.jid] = {"status": "retry_wait", "profile": "cars-old", "log": [], "dispatched_to": "windows"}
        app.FLEET_WORKERS["windows"] = {"dispatch": {}}
        response = self.client.post(f"/api/queue/{self.jid}/stop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.jid]["_stop_target"], "paused")
        self.assertEqual(app.JOBS[self.jid]["status"], "retry_wait")
        self.assertIn(self.jid, app.FLEET_WORKERS["windows"]["aborts"])

    def test_controller_restart_preserves_remote_retry_ownership(self):
        app.JOBS[self.jid] = {"status": "retry_wait", "step": "retry_wait", "profile": "cars-old",
                              "log": [], "dispatched_to": "windows", "retry_at": time.time() + 60, "retries": 1}
        app._persist_queue()
        app.JOBS.clear()
        with patch.object(app.video_render, "is_valid_video", return_value=False):
            app._restore_queue()
        self.assertEqual(app.JOBS[self.jid]["status"], "retry_wait")
        self.assertEqual(app.JOBS[self.jid]["dispatched_to"], "windows")
        self.assertEqual(app._next_queued_ids(10), [])


if __name__ == "__main__":
    unittest.main()
