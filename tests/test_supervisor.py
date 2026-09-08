"""Automatic Git update supervisor safety checks."""
import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import southcube_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        patches = [
            patch.object(supervisor, "OUTPUT", self.output),
            patch.object(supervisor, "STATE_FILE", self.output / "update_status.json"),
            patch.object(supervisor, "UPDATE_LOCK", self.output / ".southcube-update.lock"),
        ]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def test_queue_busy_blocks_every_nonterminal_state(self):
        (self.output / "queue_state.json").write_text(json.dumps({"jobs": [
            {"status": "done"}, {"status": "error"}, {"status": "queued"},
            {"status": "running"}]}))
        self.assertEqual(supervisor.queue_busy(), (True, 2))

    def test_status_is_atomic_and_redacts_credentials(self):
        supervisor.write_state("error", "Bearer abcdefghijklmnop failed")
        data = json.loads(supervisor.STATE_FILE.read_text())
        self.assertEqual(data["state"], "error")
        self.assertNotIn("abcdefghijklmnop", data["message"])
        self.assertTrue(data["auto_update"])

    def test_clean_fast_forward_is_available(self):
        def fake_git(*args, **_kwargs):
            if args[:2] == ("rev-parse", "--short=12"):
                return "local" if args[2] == "HEAD" else "remote"
            if args[:2] == ("branch", "--show-current"):
                return "main"
            if args[:2] == ("status", "--porcelain"):
                return ""
            return ""
        # origin/main is not already contained in HEAD; HEAD is contained in
        # origin/main, which is the only shape the supervisor may merge.
        results = [CompletedProcess([], 1), CompletedProcess([], 0)]
        with patch.object(supervisor, "git", side_effect=fake_git), \
                patch.object(supervisor.subprocess, "run", side_effect=results):
            result = supervisor.inspect_remote()
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["remote"], "remote")


if __name__ == "__main__":
    unittest.main()
