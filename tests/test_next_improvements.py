"""Offline regressions for the second optimization release."""
from __future__ import annotations

import array
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PIL import Image

import app
from pipeline import (ai33_tasks, artifacts, cancel, clients, config, curator,
                      fleet, footage, render, resources, storage, telemetry, voiceover)
from tests.isolation import isolated_runtime


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.context = isolated_runtime()
        self.root = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.addCleanup(cancel.set_current, None)
        cancel.set_current(None)


class ExportReliabilityTests(IsolatedTest):
    def pcm(self, name, silent=False, seconds=1):
        path = self.root / name
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            audio.writeframes(array.array("h", [0 if silent else 500] * (88200*seconds)).tobytes())
        return path

    def test_silent_master_fails_but_audible_master_passes(self):
        with self.assertRaisesRegex(RuntimeError, "silent"):
            render._validate_narration_master(self.pcm("silence.wav", True), 1, lambda m: None)
        render._validate_narration_master(self.pcm("speech.wav"), 1, lambda m: None)

    def test_short_master_cannot_cover_video(self):
        with self.assertRaisesRegex(RuntimeError, "duration"):
            render._validate_narration_master(self.pcm("short.wav"), 5, lambda m: None)

    def test_failed_extraction_preserves_source_and_never_generates_silence(self):
        source = self.root / "final.mp4"
        source.write_bytes(b"original video")
        with patch.object(render, "probe_stream_duration", return_value=2), \
                patch.object(render, "_run", side_effect=RuntimeError("decode failed")) as run:
            with self.assertRaisesRegex(RuntimeError, "prevent a silent deliverable"):
                render.encode_final_export(source, self.root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(source.read_bytes(), b"original video")

    def test_failed_new_export_is_not_promoted_to_done_after_restart(self):
        jid = "failed-export"
        job_dir = config.OUTPUT_DIR / jid
        job_dir.mkdir()
        (job_dir / "final.mp4").write_bytes(b"intermediate")
        artifacts.write(app.QUEUE_STATE_FILE, {"jobs": [{"job_id": jid, "profile": "classic",
            "status": "error", "error": "audio extraction failed", "export_validation_required": True}]})
        with patch.object(app.video_render, "is_valid_video", return_value=True), \
                patch.object(app, "_profile_uses_thumbnail", return_value=False), \
                patch.object(app, "_profile_wants_docx", return_value=False):
            app._restore_queue()
        self.assertEqual(app.JOBS[jid]["status"], "error")

    def test_one_successful_patreon_half_is_not_a_complete_render(self):
        job_dir = self.root / "split"
        job_dir.mkdir()
        part1 = job_dir / "final_part1.mp4"
        part1.write_bytes(b"valid first half")
        artifacts.publish(part1.with_suffix(".export.json"), {}, [part1])
        with patch.object(render, "is_valid_video", return_value=True):
            self.assertEqual(app._verified_delivery_roles(job_dir, {}), [])

    def test_split_delivery_resumes_after_verified_part_one_was_removed(self):
        job_dir = self.root / "split"
        job_dir.mkdir()
        outputs = [job_dir / name for name in ("final.mp4", "final_part1.mp4", "final_part2.mp4")]
        for path in outputs:
            path.write_bytes(b"verified video")
            artifacts.publish(path.with_suffix(".export.json"), {}, [path])
        artifacts.publish(job_dir / "render.manifest.json", {}, outputs)
        outputs[0].unlink()
        outputs[1].unlink()
        with patch.object(render, "is_valid_video", return_value=True):
            self.assertEqual(app._verified_delivery_roles(job_dir, {}), [])
            roles = app._verified_delivery_roles(job_dir, {"part1": {"id": "verified-remote"}})
            self.assertEqual([role for role, _ in roles], ["part1", "part2"])
            outputs[2].write_bytes(b"changed video")
            self.assertEqual(app._verified_delivery_roles(job_dir, {"part1": {"id": "verified-remote"}}), [])


class ArtifactAndNarrationTests(IsolatedTest):
    def test_changed_inputs_or_replaced_output_invalidates_contract(self):
        output = self.root / "voice.mp3"
        manifest = self.root / "manifest.json"
        output.write_bytes(b"same")
        artifacts.publish(manifest, {"voice": "one"}, [output])
        self.assertTrue(artifacts.matches(manifest, {"voice": "one"}, [output]))
        self.assertFalse(artifacts.matches(manifest, {"voice": "two"}, [output]))
        replacement = self.root / "replacement"
        replacement.write_bytes(b"same")
        replacement.replace(output)
        self.assertFalse(artifacts.matches(manifest, {"voice": "one"}, [output]))

    def test_changing_voice_regenerates_narration_even_if_old_files_play(self):
        assets = self.root / "assets"
        assets.mkdir()
        raw = assets / "tts_raw_000.mp3"
        raw.write_bytes(b"old voice")
        artifacts.publish(raw.with_suffix(".manifest.json"), {"voice": "old"}, [raw])
        (assets / "vo_000.mp3").write_bytes(b"old slice")
        def generate(text, dest, **kwargs):
            dest.write_bytes(b"new voice")
        with patch.object(render, "is_valid_audio", return_value=True), \
                patch.object(render, "probe_duration", return_value=20), \
                patch.object(voiceover, "generate_voiceover", side_effect=generate) as make, \
                patch.object(voiceover, "_slice_group"):
            voiceover.generate_voiceover_grouped([{"text": "hello"}], [(0, [0])], assets, voice_id="new")
        make.assert_called_once()
        self.assertEqual(raw.read_bytes(), b"new voice")

    def test_corrupt_manifest_is_a_cache_miss(self):
        manifest = self.root / "bad.json"
        manifest.write_text('{"incomplete":')
        self.assertFalse(artifacts.matches(manifest, {}, []))


class AI33PersistenceTests(IsolatedTest):
    def test_saved_task_is_reused_after_restart(self):
        path = self.root / "voice.task.json"
        make = Mock(return_value="task-123")
        first = ai33_tasks.submit(path, {"text": "hello"}, make, kind="tts")
        self.assertEqual(ai33_tasks.submit(path, {"text": "hello"}, make, kind="tts"), first)
        make.assert_called_once()
        self.assertEqual(len(ai33_tasks.usage()), 1)
        self.assertEqual(ai33_tasks.usage()[0]["characters"], 5)

    def test_uncertain_submission_is_never_automatically_replayed(self):
        path = self.root / "voice.task.json"
        make = Mock(side_effect=ai33_tasks.SubmissionUncertain("lost response"))
        for _ in range(2):
            with self.assertRaises(ai33_tasks.SubmissionUncertain):
                ai33_tasks.submit(path, {"text": "hello"}, make, kind="tts")
        make.assert_called_once()

    def test_definitive_voice_rejection_can_clear_only_its_saved_task(self):
        path = self.root / "voice.task.json"
        ai33_tasks.submit(path, {"text": "hello"}, lambda: "failed-task", kind="tts")
        self.assertTrue(ai33_tasks.invalidate_definitively_rejected_task(
            path, "failed-task", "elevenlabs_voice_not_found"))
        self.assertEqual(ai33_tasks.submit(
            path, {"text": "hello"}, lambda: "replacement-task", kind="tts"),
            "replacement-task")
        self.assertFalse(ai33_tasks.invalidate_definitively_rejected_task(
            path, "wrong-task", "elevenlabs_voice_not_found"))

    def test_network_timeout_sends_one_request(self):
        with patch.object(clients, "_ai33_keys", return_value=["test-key"]), \
                patch.object(clients.requests, "post", side_effect=requests.ReadTimeout("lost")) as post:
            with self.assertRaises(ai33_tasks.SubmissionUncertain):
                clients.ai33_submit_form("/v3/text-to-speech", {"text": "hello"})
        post.assert_called_once()

    def test_task_account_survives_switch_without_persisting_key(self):
        with patch.object(config, "AI33_API_KEY", "test-primary-secret"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "test-backup-secret"):
            ai33_tasks.remember("task-old", "test-primary-secret")
            with patch.object(clients, "_ai33_keys", return_value=["test-backup-secret"]):
                self.assertEqual(ai33_tasks.key_for("task-old"), "test-primary-secret")
        saved = (config.OUTPUT_DIR / "ai33_task_accounts.json").read_text()
        self.assertNotIn("test-primary-secret", saved)
        self.assertNotIn("test-backup-secret", saved)

    def test_daily_limit_includes_uncertain_reservations(self):
        with patch.object(config, "get", side_effect=lambda key, default="": "8" if key == "AI33_DAILY_CHARACTER_LIMIT" else default):
            ai33_tasks.submit(self.root / "one.json", {"text": "hello"}, lambda: "id", kind="tts")
            make = Mock()
            with self.assertRaises(ai33_tasks.UsageLimitReached):
                ai33_tasks.submit(self.root / "two.json", {"text": "hello"}, make, kind="tts")
            make.assert_not_called()

    def test_terminal_event_does_not_move_usage_to_completion_day(self):
        with patch.object(ai33_tasks, "_now", return_value="2026-09-04T23:00:00+00:00"):
            ai33_tasks.submit(self.root / "one.json", {"text": "hello"}, lambda: "id", kind="tts")
        ai33_tasks.complete("id", {})
        self.assertTrue(ai33_tasks.usage()[0]["at"].startswith("2026-09-04"))


class StockPreparationTests(IsolatedTest):
    def hit(self, nid):
        def download(path):
            Image.new("RGB", (16, 16), "red").save(path, format="JPEG")
            return path
        return footage.Hit(nid, 1, "engine", "photo", 0, None, download)

    def test_restored_stock_pick_prevents_repetition(self):
        chosen = self.root / "beat_000.jpg"
        footage._select([self.hit("asset-a")], chosen, 0.4, {}, "engine", "engine", "off")
        used = footage.restore_used([{"media": chosen}])
        self.assertEqual(used, {"asset-a": 1})
        result = footage._select([self.hit("asset-a"), self.hit("asset-b")],
                                  self.root / "beat_001.jpg", 0.4, used, "engine", "engine", "off")
        self.assertEqual(artifacts.read(result.with_suffix(".stock.json"))["nid"], "asset-b")

    def test_prefetch_overlaps_three_searches_but_not_selection(self):
        barrier = threading.Barrier(3)
        def search(query, kind, width):
            barrier.wait(timeout=2)
            return query
        beats = [{"type": "stock", "query": str(i)} for i in range(3)]
        with patch.object(footage, "candidates", side_effect=search), \
                patch.object(footage, "_select") as select:
            prefetch = footage.Prefetch(beats, 1920)
            try:
                self.assertEqual(prefetch.at(0).result(timeout=3), "0")
                self.assertEqual(prefetch.at(1).result(timeout=3), "1")
                self.assertEqual(prefetch.at(2).result(timeout=3), "2")
            finally:
                prefetch.close()
            select.assert_not_called()

    def test_cancelled_prefetch_never_contacts_provider(self):
        token = cancel.CancelToken("cancelled")
        token.cancel()
        cancel.set_current(token)
        prefetch = footage.Prefetch([{"type": "stock", "query": "engine"}], 1920)
        with patch.object(footage, "_gather") as gather:
            try:
                with self.assertRaises(cancel.JobCancelled):
                    prefetch.at(0).result()
            finally:
                prefetch.close()
        gather.assert_not_called()

    def test_curator_context_cache_does_not_charge_twice(self):
        with patch.object(curator, "_ask", return_value={"pick": 0}) as ask:
            hit = self.hit("first")
            self.assertEqual(curator.choose("engine", "engine", [hit], "text"), 0)
            self.assertEqual(curator.choose("engine", "engine", [hit], "text"), 0)
            self.assertEqual(ask.call_count, 1)
            curator.choose("battery", "engine", [hit], "text")
            self.assertEqual(ask.call_count, 2)


class WorkerAndBudgetTests(IsolatedTest):
    def test_encoder_limit_is_shared_across_jobs(self):
        release = threading.Event()
        entered = threading.Event()
        second_entered = threading.Event()
        def first():
            with resources.encode_slot():
                entered.set()
                release.wait(3)
        def second():
            with resources.encode_slot():
                second_entered.set()
        with patch.object(resources, "limit", return_value=1), \
                patch.object(resources, "available_memory_gb", return_value=8), \
                ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            self.assertTrue(entered.wait(2))
            b = pool.submit(second)
            self.assertFalse(second_entered.wait(0.1))
            release.set()
            a.result(timeout=2)
            b.result(timeout=2)
            self.assertTrue(second_entered.is_set())

    def test_pending_encoder_can_be_cancelled(self):
        token = cancel.CancelToken("waiting")
        token.cancel()
        cancel.set_current(token)
        with patch.object(resources, "limit", return_value=1), \
                patch.object(resources, "available_memory_gb", return_value=8):
            with self.assertRaises(cancel.JobCancelled), resources.encode_slot():
                self.fail("cancelled encode acquired a slot")

    def test_old_attempt_ack_cannot_remove_new_attempt(self):
        worker = fleet.FleetWorker(lambda *_: None)
        current = fleet.FleetReporter("job", "new")
        current.complete({})
        worker._jobs["job"] = current
        worker._handle({"acknowledged_attempts": [{"job_id": "job", "attempt_id": "old"}]})
        self.assertIn("job", worker._jobs)
        worker._handle({"acknowledged_attempts": [{"job_id": "job", "attempt_id": "new"}]})
        self.assertNotIn("job", worker._jobs)

    def test_worker_restart_restores_attempt_without_replaying_work(self):
        worker = fleet.FleetWorker(lambda *_: self.fail("must not execute"))
        worker._jobs["job"] = fleet.FleetReporter("job", "one")
        worker._persist_reporters()
        restored = fleet.FleetWorker(lambda *_: self.fail("must not execute"))
        restored._handle({"dispatch": [{"job_id": "job", "attempt_id": "one"}]})
        self.assertEqual(restored._jobs["job"].snapshot()["status"], "error")

    def test_stale_report_cannot_complete_current_attempt(self):
        app.JOBS["job"] = {"profile": "classic", "title": "Test", "status": "dispatched",
                           "dispatched_to": "win", "attempt_id": "new", "log": []}
        app.FLEET_WORKERS["win"] = {"dispatch": {"job": {"attempt_id": "new"}}}
        with patch.object(app.config, "FLEET_ROLE", "main"), \
                patch.object(app.config, "FLEET_PAIRING_TOKEN", "test-token"), \
                patch.object(app, "_fleet_dispatch_for", return_value=[]):
            response = app.app.test_client().post("/api/fleet/heartbeat", headers={"Authorization": "Bearer test-token"},
                json={"worker_id": "win", "protocol_version": 3, "capacity": {"max_jobs": 2, "free": 2},
                      "jobs": [{"job_id": "job", "attempt_id": "old", "status": "done"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS["job"]["status"], "dispatched")
        self.assertIn("job", app.FLEET_WORKERS["win"]["dispatch"])


class DeliveryTests(IsolatedTest):
    def test_changed_same_size_source_cannot_resume_old_upload(self):
        source = self.root / "Titles.docx"
        source.write_bytes(b"old bytes")
        state_path = self.root / "upload.json"
        state = {"source_name": source.name, "size": source.stat().st_size,
                 "source_signature": artifacts.signature(source), "session_url": "session"}
        artifacts.write(state_path, state)
        self.assertEqual(storage.GoogleDriveStorage._load_upload_state(state_path, source, 9), state)
        source.write_bytes(b"new bytes")
        self.assertEqual(storage.GoogleDriveStorage._load_upload_state(state_path, source, 9), {})

    def test_progress_counts_confirmed_bytes_and_reports_verification(self):
        source = self.root / "video.mp4"
        source.write_bytes(b"12345678")
        drive = storage.GoogleDriveStorage(chunk_bytes=4)
        first = SimpleNamespace(status_code=308, headers={"Range": "bytes=0-3"})
        last = SimpleNamespace(status_code=200, json=lambda: {"id": "remote", "size": "8"})
        updates = []
        with patch.object(drive, "_request", side_effect=[first, last]), \
                patch.object(drive, "_probe_session", return_value=(3, None)):
            result = drive._send_chunks(source, 8, "session", self.root / "upload.json", {}, 0,
                                       on_progress=updates.append)
        self.assertEqual(result["id"], "remote")
        self.assertEqual([u["confirmed_bytes"] for u in updates], [0, 4, 8])
        self.assertEqual(updates[-1]["phase"], "verifying")

    def test_permission_failure_does_not_retry_or_delete_source(self):
        source = self.root / "video.mp4"
        source.write_bytes(b"1234")
        drive = storage.GoogleDriveStorage(chunk_bytes=4)
        with patch.object(drive, "_request", return_value=SimpleNamespace(status_code=403)) as request:
            with self.assertRaisesRegex(storage.DriveError, "access denied"):
                drive._send_chunks(source, 4, "session", self.root / "upload.json", {}, 0)
        request.assert_called_once()
        self.assertTrue(source.exists())


@unittest.skipUnless(os.environ.get("DMIQWI_RENDER_FFMPEG_TESTS") == "1" and shutil.which("ffmpeg"), "opt-in FFmpeg export comparison")
class RealExportTests(IsolatedTest):
    def test_popup_merged_export_matches_separate_pass_and_keeps_audio(self):
        source = self.root / "source.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True)
        popup = self.root / "popup.png"
        Image.new("RGBA", (80, 40), (255, 0, 0, 255)).save(popup)
        merged, legacy = self.root / "merged.mp4", self.root / "legacy.mp4"
        shutil.copy2(source, merged)
        shutil.copy2(source, legacy)
        with patch.object(resources, "available_memory_gb", return_value=8):
            render.burn_timed_popup(legacy, popup, legacy, [(0.5, 1.5)], popup_width=80, popup_margin=10, workdir=self.root)
            render.encode_final_export(legacy, self.root)
            render.encode_final_export(merged, self.root, popup=popup, popup_windows=[(0.5, 1.5)], popup_width=80, popup_margin=10)
        for path in (merged, legacy):
            contract = render._validate_spotify_export(path, 30)
            self.assertGreater(float(contract["audio"]["duration"]), 1.9)
        def pixel(path, at):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path),
                                  "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
            i = (140 * 320 + 280) * 3
            return tuple(raw[i:i+3])
        for at in (0.2, 0.8, 1.8):
            a, b = pixel(merged, at), pixel(legacy, at)
            self.assertLess(max(abs(x-y) for x, y in zip(a, b)), 18)
        self.assertGreater(pixel(merged, 0.8)[0], 200)
        self.assertGreater(pixel(merged, 1.8)[2], 200)
