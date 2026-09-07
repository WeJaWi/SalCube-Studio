"""Metadata-only changes must not block valid exports or hide corruption."""
import os
import unittest
from unittest.mock import Mock, patch

import app
from pipeline import artifacts, config, render, storage
from tests.isolation import isolated_runtime


class UploadVerificationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_runtime()
        self.root = self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.video = self.root / "final.mp4"
        self.video.write_bytes(b"verified video bytes")
        self.receipt = self.video.with_suffix(".export.json")

    def publish(self, hashed=True):
        artifacts.publish(self.receipt, {}, [self.video], hash_outputs=hashed)
        artifacts.publish(self.root / "render.manifest.json", {}, [self.video])

    def test_metadata_only_drift_accepted_with_matching_hash(self):
        self.publish()
        saved = artifacts.signature(self.video)
        drift = [*saved[:3], saved[3] + 1]
        with patch.object(artifacts, "signature", return_value=drift), \
                patch.object(render, "is_valid_video", return_value=True):
            self.assertTrue(artifacts.output_matches(artifacts.read(self.receipt), self.video))
            self.assertEqual(app._verified_delivery_roles(self.root, {}), [("final", self.video)])

    def test_legacy_receipt_with_changed_ctime_still_fails_closed(self):
        self.publish(hashed=False)
        saved = artifacts.signature(self.video)
        with patch.object(artifacts, "signature", return_value=[*saved[:3], saved[3] + 1]):
            self.assertFalse(artifacts.output_matches(artifacts.read(self.receipt), self.video))

    def test_same_size_rewrite_with_restored_mtime_is_rejected(self):
        self.publish()
        previous = self.video.stat()
        self.video.write_bytes(b"different video data")
        os.utime(self.video, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(self.video.stat().st_size, previous.st_size)
        self.assertFalse(artifacts.output_matches(artifacts.read(self.receipt), self.video))

    def test_hash_does_not_override_size_or_mtime_mismatch(self):
        self.publish()
        saved = artifacts.signature(self.video)
        with patch.object(artifacts, "signature", return_value=[saved[0], saved[1], saved[2]+1, saved[3]]):
            self.assertFalse(artifacts.output_matches(artifacts.read(self.receipt), self.video))

    def test_file_changed_during_hash_is_rejected(self):
        saved = artifacts.signature(self.video)
        with patch.object(artifacts, "signature", side_effect=[saved, [saved[0], saved[1], saved[2]+1, saved[3]]]):
            with self.assertRaisesRegex(OSError, "changed"):
                artifacts.file_digest(self.video)

    def test_hash_rechecks_when_indexing_changes_ctime_during_first_read(self):
        saved = artifacts.signature(self.video)
        drift = [*saved[:3], saved[3]+1]
        expected = artifacts.file_digest(self.video)
        with patch.object(artifacts, "signature", side_effect=[saved, drift, drift]):
            self.assertEqual(artifacts.file_digest(self.video), expected)

    def test_invalid_grant_shows_reconnect_and_stops_repeated_refreshes(self):
        drive = storage.GoogleDriveStorage(token_file=config.OUTPUT_DIR / "token.json")
        drive._token = {"refresh_token": "test-refresh", "expires_at": 0}
        drive._client = {"client_id": "test", "client_secret": "test", "token_uri": "https://example.invalid/token"}
        response = Mock(status_code=400)
        response.json.return_value = {"error": "invalid_grant"}
        with patch.object(storage.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(storage.DriveAuthorizationRequired, "Reconnect"):
                drive._access_token()
            self.assertFalse(drive.authorized)
            with self.assertRaises(storage.DriveAuthorizationRequired):
                drive._access_token()
            self.assertEqual(post.call_count, 1)
        drive._replace_token({"refresh_token": "new-test-token"})
        self.assertTrue(drive.authorized)

    def test_auth_failure_keeps_partial_remote_delivery_records(self):
        app.JOBS["test"] = {"status": "done", "drive": {"files": {"part1": {"id": "remote"}}}}
        with patch.object(app, "DRIVE", Mock(configured=True, authorized=False)), \
                patch.object(app, "_persist_queue"), patch.object(app, "_append_job_log"):
            app._upload_job_deliverables("test", self.root)
        self.assertEqual(app.JOBS["test"]["drive"]["files"], {"part1": {"id": "remote"}})

    def test_cannot_retry_upload_of_a_running_render(self):
        app.JOBS["test"] = {"profile": "classic", "status": "running"}
        with patch.object(app, "ACCESS", []):
            response = app.app.test_client().post("/api/drive/retry/test")
        self.assertEqual(response.status_code, 409)

    def test_cannot_disconnect_during_upload_retry_of_done_job(self):
        app.JOBS["test"] = {"status": "done", "drive": {"state": "uploading"}}
        with patch.object(app, "ACCESS", []), patch.object(app.DRIVE, "disconnect") as disconnect:
            response = app.app.test_client().post("/api/drive/disconnect")
        self.assertEqual(response.status_code, 409)
        disconnect.assert_not_called()

    def test_metadata_only_change_uploads_existing_video_without_rerender(self):
        self.publish()
        saved = artifacts.signature(self.video)
        drift = [*saved[:3], saved[3]+1]
        app.JOBS["test"] = {"status": "done", "profile": "classic", "export_validation_required": True}
        drive = Mock(configured=True, authorized=True)
        drive.find_file.return_value = None
        drive.upload.return_value = {"id": "verified-remote", "name": "video.mp4"}
        with patch.object(app, "DRIVE", drive), patch.object(artifacts, "signature", return_value=drift), \
                patch.object(render, "is_valid_video", return_value=True), \
                patch.object(app, "_persist_queue"), patch.object(app, "_append_job_log"), \
                patch.object(app, "_upload_profile_docx"):
            app._upload_job_deliverables("test", self.root)
        drive.upload.assert_called_once()
        self.assertEqual(app.JOBS["test"]["drive"]["state"], "uploaded")
        self.assertFalse(self.video.exists())
