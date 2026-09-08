"""Central SouthCube monitoring contracts and privacy boundaries."""
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from pipeline import cluster, config, node_monitor
from tests.isolation import isolated_runtime


def node(worker_id="worker", *, online=True, instance="one"):
    return {"worker_id": worker_id, "name": worker_id, "role": "worker",
            "online": online, "stale": not online,
            "health": "available" if online else "offline", "capacity": {"free": 1},
            "node": {"instance_id": instance, "changes": []}}


class ClusterHistoryTests(unittest.TestCase):
    def test_transitions_survive_reload_without_claiming_a_crash(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            history = cluster.ClusterHistory(path)
            history.observe([node()], [{"job_id": "1", "title": "Video", "worker_id": "worker",
                                        "status": "running", "step": "render"}])
            history.observe([node(instance="two")], [{"job_id": "1", "title": "Video",
                                                       "worker_id": "worker", "status": "error",
                                                       "step": "error", "error": "encoder failed"}])
            result = history.observe([], [])
            kinds = [event["kind"] for event in result["events"]]
            self.assertIn("node_restarted", kinds)
            self.assertIn("heartbeat_lost", kinds)
            self.assertIn("job_status", kinds)
            self.assertNotIn("crash", " ".join(event["message"].lower() for event in result["events"]))
            restored = cluster.ClusterHistory(path)
            self.assertFalse(restored.known_workers()[0]["online"])
            self.assertEqual(restored.observe([], [])["job_history"][0]["status"], "error")

    def test_sanitizer_redacts_fields_and_values(self):
        node_monitor._known_secret_values.cache_clear()
        clean = node_monitor.sanitize({"api_key": "example-secret", "message": "Bearer abcdefghijklmno",
                                       "nested": [{"password": "hidden"}], "worker_id": "w"})
        self.assertEqual(clean["api_key"], "[redacted]")
        self.assertEqual(clean["nested"][0]["password"], "[redacted]")
        self.assertNotIn("abcdefghijklmno", clean["message"])
        self.assertEqual(clean["worker_id"], "w")

    def test_file_changes_ignore_timestamp_only_rewrites(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / "pipeline").mkdir(); (root / "output").mkdir()
            source = root / "app.py"; source.write_text("one")
            monitor = object.__new__(node_monitor.NodeMonitor)
            monitor.root = root; monitor.output_dir = root / "output"
            monitor.instance_id = "test"; monitor._file_state = None
            self.assertEqual(monitor._changes(), [])
            source.touch()
            self.assertEqual(monitor._changes(), [])
            source.write_text("two")
            changes = monitor._changes()
            self.assertEqual(changes[0]["path"], "app.py")
            self.assertEqual(changes[0]["kind"], "modified")


class ClusterApiTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_runtime()
        self.root = scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.history = cluster.ClusterHistory(config.OUTPUT_DIR / "cluster_history.json")
        self.monitor = type("Monitor", (), {"snapshot": lambda _self: {
            "sampled_at": "2026-09-08T12:00:00Z", "instance_id": "main-one",
            "state": "ready", "cpu": {"percent": 12, "cores": 8},
            "memory": {"used_percent": 34}, "gpu": {"available": False, "devices": []},
            "processes": [], "changes": []}})()

    def test_cluster_api_combines_main_worker_jobs_and_history(self):
        app.JOBS["123"] = {"title": "Queued video", "profile": "classic",
                           "status": "queued", "step": "queued", "progress_pct": 0,
                           "created_at": "2026-09-08T10:00:00"}
        app.FLEET_WORKERS["w"] = {"worker_id": "w", "name": "Linux", "protocol_version": 5,
            "last_seen": datetime.now().isoformat(timespec="seconds"), "hardware": {},
            "capacity": {"max_jobs": 1, "free": 1}, "metrics": {}, "node": {},
            "jobs": [], "dispatch": {}}
        with patch.object(app, "ACCESS", []), patch.object(app, "_node_monitor", return_value=self.monitor), \
                patch.object(app, "_cluster_history", return_value=self.history):
            response = app.app.test_client().get("/api/cluster")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["name"], "SouthCube")
        self.assertEqual([row["worker_id"] for row in body["nodes"]], ["main", "w"])
        self.assertEqual(body["job_history"][0]["job_id"], "123")
        self.assertEqual(body["summary"]["total"], 2)

    def test_heartbeat_accepts_telemetry_but_redacts_credentials(self):
        client = app.app.test_client()
        with patch.object(app, "ACCESS", []), patch.object(config, "FLEET_ROLE", "main"), \
                patch.object(config, "FLEET_PAIRING_TOKEN", "test-fleet-token"), \
                patch.object(app, "_persist_queue"):
            response = client.post("/api/fleet/heartbeat",
                headers={"Authorization": "Bearer test-fleet-token"}, json={
                    "worker_id": "w", "capacity": {"max_jobs": 1, "free": 0},
                    "node": {"instance_id": "abc", "api_key": "must-not-survive",
                             "cpu": {"percent": 9}}, "jobs": []})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("api_key", app.FLEET_WORKERS["w"]["node"])
        self.assertEqual(app.FLEET_WORKERS["w"]["node"]["cpu"]["percent"], 9)

    def test_scoped_account_cannot_open_central_cluster(self):
        with patch.object(app, "ACCESS", [{"key": "scoped", "profiles": ["classic"]}]):
            client = app.app.test_client(); client.set_cookie("studio_key", "scoped")
            self.assertEqual(client.get("/api/cluster").status_code, 403)
            self.assertEqual(client.get("/cluster").status_code, 403)

    def test_update_lock_keeps_queued_work_from_starting(self):
        app.JOBS["queued"] = {"status": "queued", "step": "queued",
                              "profile": "classic", "inputs": {}, "created_at": "now"}
        app.UPDATE_LOCK_FILE.touch()
        with patch.object(app.threading, "Thread") as thread:
            app._kick_queue()
        self.assertEqual(app.JOBS["queued"]["status"], "queued")
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
