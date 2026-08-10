from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app
from PIL import Image
from docx import Document
from pipeline import (delivery_doc, identity, llm, operations, orchestrator,
                      render, research, script, storage, thumbnail)


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

    def test_geistmeister_spotify_style_is_persisted(self):
        inputs = app._build_inputs({
            "video_group": "small", "spotify_popup": True,
            "spotify_popup_style": "geistmeister",
        }, "A title")
        self.assertTrue(inputs["spotify_popup"])
        self.assertEqual(inputs["spotify_popup_style"], "geistmeister")


class ResearchProfileTests(unittest.TestCase):
    def setUp(self):
        self.old_access = app.ACCESS
        app.ACCESS = []
        app.JOBS.clear()
        self.tmp = TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.output_patch = patch.object(app.config, "OUTPUT_DIR", self.out)
        self.output_patch.start()
        self.client = app.app.test_client()

    def tearDown(self):
        app.JOBS.clear()
        app.ACCESS = self.old_access
        self.output_patch.stop()
        self.tmp.cleanup()

    def test_direct_reddit_skips_research_and_keeps_submitted_title(self):
        submitted = "My Family Cut Me Off Over Money — Then I Learned Why"
        inputs = app._build_inputs({"video_group": "small"}, submitted)
        with patch.object(app.identity, "numeric_id",
                          return_value="11111111111111111111"), \
                patch.object(app, "_start_profile_research") as research_start, \
                patch.object(app, "_start_thumbnail_words") as thumbnail_start, \
                patch.object(app, "_persist_queue"):
            job_id = app._enqueue_job(inputs, "reddit")
        job = app.JOBS[job_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["title"], submitted)
        self.assertEqual(job["thumbnail"]["state"], "generating_words")
        self.assertEqual(job["inputs"]["language"], "English")
        self.assertGreaterEqual(job["inputs"]["length_chars"], 40000)
        self.assertLessEqual(job["inputs"]["length_chars"], 70000)
        self.assertEqual(job["video_group"], "profile")
        research_start.assert_not_called()
        thumbnail_start.assert_called_once_with(job_id)

    def test_direct_astrology_skips_research_and_keeps_submitted_title(self):
        submitted = "The Three Zodiac Signs Entering a New Chapter"
        inputs = app._build_inputs({"video_group": "small"}, submitted)
        with patch.object(app.identity, "numeric_id",
                          return_value="44444444444444444444"), \
                patch.object(app, "_start_profile_research") as research_start, \
                patch.object(app, "_start_thumbnail_words") as thumbnail_start, \
                patch.object(app, "_persist_queue"):
            job_id = app._enqueue_job(inputs, "astrology")
        job = app.JOBS[job_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["title"], submitted)
        self.assertEqual(job["inputs"]["language"], "English")
        self.assertGreaterEqual(job["inputs"]["length_chars"], 40000)
        self.assertLessEqual(job["inputs"]["length_chars"], 70000)
        research_start.assert_not_called()
        thumbnail_start.assert_called_once_with(job_id)

    def test_direct_astrology_thumbnail_cannot_rewrite_submitted_title(self):
        job_id = "55555555555555555555"
        submitted = "Your Moon Sign and the Patterns You Repeat"
        app.JOBS[job_id] = {
            "status": "queued", "step": "queued", "title": submitted,
            "profile": "astrology", "inputs": {"language": "English"},
            "log": [], "thumbnail": {"state": "generating_words"},
        }
        package = {
            "localized_title": "Terra Tried to Replace the Title",
            "main_text": "Notice the repeating pattern",
            "red_word": "Moon Sign", "yellow_word": "Inner Cycle",
            "name": "ASTROLOGY",
        }
        with patch.object(app.llm, "chat_json", return_value=package), \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(job_id)
        self.assertEqual(
            app.JOBS[job_id]["thumbnail"]["localized_title"], submitted)

    def test_title_approval_accepts_only_a_terra_candidate_and_queues_video(self):
        job_id = "22222222222222222222"
        job_dir = self.out / job_id
        job_dir.mkdir()
        candidates = ["Title One", "Title Two", "Title Three"]
        app.JOBS[job_id] = {
            "status": "awaiting_title", "step": "title", "title": "theme",
            "profile": "astrology-search", "inputs": {"title": "theme"}, "log": [],
            "title_options": {"state": "ready", "theme": "theme",
                              "candidates": candidates},
            "thumbnail": {"state": "waiting_for_title"},
        }
        with patch.object(app, "_persist_queue"), \
                patch.object(app, "_start_thumbnail_words") as thumb, \
                patch.object(app, "_kick_queue") as kick:
            rejected = self.client.post(
                f"/api/title/job/{job_id}/approve", json={"title": "Other"})
            accepted = self.client.post(
                f"/api/title/job/{job_id}/approve", json={"title": "Title Two"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(app.JOBS[job_id]["status"], "queued")
        self.assertEqual(app.JOBS[job_id]["publication_title"], "Title Two")
        self.assertEqual(app.JOBS[job_id]["thumbnail"]["state"],
                         "generating_words")
        saved = json.loads((job_dir / "inputs.json").read_text())
        self.assertEqual(saved["title"], "Title Two")
        thumb.assert_called_once_with(job_id)
        kick.assert_called_once()

    def test_direct_reddit_thumbnail_cannot_rewrite_submitted_title(self):
        job_id = "33333333333333333333"
        app.JOBS[job_id] = {
            "status": "queued", "step": "queued", "title": "Chosen Title",
            "publication_title": "Chosen Title", "profile": "reddit",
            "inputs": {"language": "English"}, "log": [],
            "thumbnail": {"state": "generating_words"},
        }
        package = {"localized_title": "Terra Changed It",
                   "main_text": "THE TRUTH CAME OUT", "red_word": "TOO LATE",
                   "yellow_word": "ONE DECISION", "name": "REDDIT STORIES"}
        with patch.object(app.llm, "chat_json", return_value=package), \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(job_id)
        self.assertEqual(app.JOBS[job_id]["thumbnail"]["localized_title"],
                         "Chosen Title")

    def test_reddit_html_parser_extracts_submission_not_sidebar(self):
        page = ("<div class=\"usertext-body may-blank-within md-container\">"
                "<div class=\"md\"><p>sidebar</p></div></div>"
                "<div class=\"thing id-t3_post\">"
                "<div class=\"usertext-body may-blank-within md-container\">"
                "<div class=\"md\"><p>The actual story begins.</p>"
                "<p>It has an update.</p></div></div></div>")
        text = research._main_post_text(page)
        self.assertIn("The actual story begins.", text)
        self.assertIn("It has an update.", text)
        self.assertNotIn("sidebar", text)


class DeliveryDocumentTests(unittest.TestCase):
    def test_docx_contains_title_and_embedded_thumbnail(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "thumb.png"
            Image.new("RGB", (1280, 720), (35, 29, 48)).save(image_path)
            out = delivery_doc.create(
                "Three Signs Your Life Is Entering a New Cycle",
                image_path, root / "package.docx")
            doc = Document(out)
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("Three Signs Your Life Is Entering a New Cycle", text)
            self.assertEqual(len(doc.inline_shapes), 1)
            self.assertGreater(out.stat().st_size, 1000)

    def test_drive_session_uses_docx_content_type(self):
        drive = storage.GoogleDriveStorage()
        response = type("Response", (), {
            "status_code": 200, "headers": {"Location": "https://session"},
        })()
        with patch.object(drive, "_request", return_value=response) as request_call:
            session = drive._start_session(
                "package.docx", 1234, "folder",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(session, "https://session")
        headers = request_call.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Upload-Content-Type"],
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class SpotifyPopupTests(unittest.TestCase):
    def test_geistmeister_selects_dedicated_asset(self):
        asset = orchestrator._spotify_popup_asset({
            "spotify_popup_style": "geistmeister", "language": "German",
        })
        self.assertEqual(asset, "branding/spotify-popup-geistmeister.png")
        self.assertTrue((orchestrator.config.ROOT / asset).is_file())

    def test_standard_popup_remains_language_specific(self):
        self.assertEqual(orchestrator._spotify_popup_asset({
            "spotify_popup_style": "standard", "language": "German",
        }), "branding/spotify-popup-de.png")
        self.assertEqual(orchestrator._spotify_popup_asset({
            "spotify_popup_style": "standard", "language": "Dutch",
        }), "branding/spotify-popup-nl.png")

    def test_geistmeister_script_cue_names_the_artist(self):
        cue = script._spotify_line({
            "spotify_popup": True,
            "spotify_popup_style": "geistmeister",
        }, {})
        self.assertIn("GEISTMEISTER", cue)

    def test_geistmeister_window_covers_complete_export(self):
        self.assertEqual(render.resolve_popup_windows(
            321.5, [(0.0, 10.0)], full_duration=True),
            [(0.0, 322.5)])

    def test_standard_window_remains_timed(self):
        requested = [(0.0, 10.0), (120.0, 130.0)]
        self.assertEqual(render.resolve_popup_windows(
            321.5, requested, full_duration=False), requested)


class MacRenderOptimizationTests(unittest.TestCase):
    def test_videotoolbox_uses_measured_m4_speed_hints(self):
        with patch.object(render.config, "gpu_encoder",
                          return_value="h264_videotoolbox"):
            args = render._encode_args(crf=20, gpu=True)
        self.assertEqual(args[0:2], ["-c:v", "h264_videotoolbox"])
        self.assertIn("-realtime", args)
        self.assertIn("-prio_speed", args)
        self.assertIn("-power_efficient", args)
        self.assertEqual(args[args.index("-q:v") + 1], "65")

    def test_partial_mp4_is_not_a_valid_deliverable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.mp4"
            path.write_bytes(b"x" * 20_000)
            with patch.object(render, "probe_duration", return_value=0):
                self.assertFalse(render.is_valid_video(path))
            with patch.object(render, "probe_duration", return_value=12.5):
                self.assertTrue(render.is_valid_video(path))

    def test_hierarchical_crossfade_never_exceeds_input_bound(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = [root / f"beat_{i:03d}.mp4" for i in range(156)]
            calls = []

            def record(group, _music, out, *_args, **_kwargs):
                calls.append(len(group))
                out.write_bytes(b"x" * 20_000)

            with patch.object(render, "_xfade_chain", side_effect=record), \
                    patch.object(render, "is_valid_video", return_value=False):
                render.crossfade_concat(
                    clips, None, root / "final.mp4", 0.5,
                    ["fade", "smoothleft"], workdir=root)
        self.assertTrue(calls)
        self.assertLessEqual(max(calls), render.MAX_XFADE_INPUTS)
        self.assertEqual(calls[-1], 2)


class QueueRepairTests(unittest.TestCase):
    def setUp(self):
        self.old_access = app.ACCESS
        app.ACCESS = []
        app.JOBS.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app.JOBS.clear()
        app.FLEET_WORKERS.clear()
        app.ACCESS = self.old_access

    def test_removed_job_disappears_and_is_not_persisted(self):
        job_id = "removed-job"
        app.JOBS[job_id] = {
            "status": "error", "step": "error", "title": "Old failure",
            "profile": "reddit", "inputs": {}, "log": [],
        }
        with patch.object(app, "_persist_queue") as persist:
            response = self.client.delete(f"/api/queue/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[job_id]["status"], "cancelled")
        persist.assert_called_once()
        rows = self.client.get("/api/jobs").get_json()
        self.assertNotIn(job_id, {row["job_id"] for row in rows})

    def test_healthy_fleet_report_clears_stale_job_error(self):
        job_id = "recovered-job"
        worker_id = "worker-one"
        app.JOBS[job_id] = {
            "status": "running", "step": "render", "progress_pct": 20,
            "title": "Recovered", "profile": "classic", "inputs": {},
            "log": [], "dispatched_to": worker_id, "error": "old error",
        }
        app.FLEET_WORKERS[worker_id] = {"approvals": [], "dispatch": {}}
        with patch.object(app.config, "FLEET_ROLE", "main"), \
                patch.object(app.config, "FLEET_PAIRING_TOKEN", "token"), \
                patch.object(app, "_persist_queue"), \
                patch.object(app, "_fleet_dispatch_for", return_value=[]):
            response = self.client.post(
                "/api/fleet/heartbeat",
                headers={"Authorization": "Bearer token"},
                json={"worker_id": worker_id, "capacity": {"free": 0},
                      "jobs": [{"job_id": job_id, "status": "running",
                                "step": "render", "progress_pct": 40}]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("error", app.JOBS[job_id])
        self.assertEqual(app.JOBS[job_id]["progress_pct"], 40)

    def test_remote_render_does_not_consume_a_mac_slot(self):
        app.JOBS.update({
            "local": {"status": "running"},
            "remote": {"status": "running", "dispatched_to": "worker-one"},
        })
        self.assertEqual(app._active_slot_count(), 1)

    def test_local_only_retry_is_not_claimed_by_fleet(self):
        app.JOBS.update({
            "local-retry": {
                "status": "queued", "profile": "reddit", "inputs": {},
                "local_only": True, "created_at": "2026-08-08T12:00:00",
            },
            "fleet-job": {
                "status": "queued", "profile": "reddit", "inputs": {},
                "created_at": "2026-08-08T12:01:00",
            },
            "local-running": {"status": "running"},
            "local-running-2": {"status": "running"},
        })
        with patch.object(app.config, "FLEET_ROLE", "main"), \
                patch.object(app.config, "max_concurrent_jobs", return_value=2), \
                patch.object(app.orchestrator, "load_profile", return_value={}):
            dispatch = app._fleet_dispatch_for("worker-one", 1)
        self.assertEqual([row["job_id"] for row in dispatch], ["fleet-job"])
        self.assertEqual(app.JOBS["local-retry"]["status"], "queued")

    def test_controller_restart_keeps_running_job_on_its_worker(self):
        with TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "queue_state.json"
            state_file.write_text(json.dumps({"version": 1, "jobs": [{
                "job_id": "remote-job", "status": "running", "step": "render",
                "title": "Remote", "profile": "classic", "inputs": {},
                "created_at": "2026-08-08T12:00:00", "progress_pct": 72,
                "dispatched_to": "worker-one", "log": [],
            }]}))
            with patch.object(app, "QUEUE_STATE_FILE", state_file), \
                    patch.object(app.config, "OUTPUT_DIR", Path(tmp)), \
                    patch.object(app, "_persist_queue"), \
                    patch.object(app, "_kick_queue") as kick:
                app._restore_queue()
        self.assertEqual(app.JOBS["remote-job"]["status"], "dispatched")
        self.assertEqual(app.JOBS["remote-job"]["dispatched_to"], "worker-one")
        self.assertEqual(app.JOBS["remote-job"]["progress_pct"], 72)
        kick.assert_called_once()

    def test_controller_does_not_restore_corrupt_final_as_done(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "broken-job"
            job_dir.mkdir()
            (job_dir / "final.mp4").write_bytes(b"partial container")
            state_file = root / "queue_state.json"
            state_file.write_text(json.dumps({"version": 1, "jobs": [{
                "job_id": "broken-job", "status": "error", "step": "error",
                "title": "Broken", "profile": "reddit", "inputs": {},
                "created_at": "2026-08-08T12:00:00", "progress_pct": 93,
                "error": "final assembly failed", "log": [],
            }]}))
            with patch.object(app, "QUEUE_STATE_FILE", state_file), \
                    patch.object(app.config, "OUTPUT_DIR", root), \
                    patch.object(app.video_render, "is_valid_video", return_value=False), \
                    patch.object(app, "_persist_queue"), \
                    patch.object(app, "_kick_queue"):
                app._restore_queue()
        self.assertEqual(app.JOBS["broken-job"]["status"], "error")
        self.assertEqual(app.JOBS["broken-job"]["error"], "final assembly failed")

    def test_started_manual_retry_does_not_preempt_another_job(self):
        retry_id = "retry-job"
        app.JOBS["other-job"] = {
            "status": "running", "step": "script", "profile": "reddit",
            "title": "Other", "inputs": {}, "log": [],
        }

        def start_retry(*_args, **_kwargs):
            app.JOBS[retry_id] = {
                "status": "running", "step": "outline", "profile": "reddit",
                "title": "Retry", "inputs": {}, "log": [],
                "est_chars": 50000, "video_group": "profile",
            }
            return retry_id

        with patch.object(app, "_enqueue_job", side_effect=start_retry), \
                patch.object(app, "_preempt_for") as preempt, \
                patch.object(app.config, "max_concurrent_jobs", return_value=2):
            response = self.client.post(
                "/api/render", json={"resume_job": retry_id, "profile": "reddit"})
        self.assertEqual(response.status_code, 200)
        preempt.assert_not_called()
        self.assertEqual(app.JOBS["other-job"]["status"], "running")

    def test_local_only_retry_preempts_even_when_fleet_has_space(self):
        retry_id = "local-retry"
        app.JOBS["other-job"] = {
            "status": "running", "step": "broll", "profile": "reddit",
            "title": "Other", "inputs": {}, "log": [],
        }
        app.FLEET_WORKERS["worker-one"] = {
            "last_seen": "2099-01-01T00:00:00",
            "capacity": {"free": 1}, "approvals": [], "dispatch": {},
        }

        def queue_retry(inputs, *_args, **_kwargs):
            app.JOBS[retry_id] = {
                "status": "queued", "step": "queued", "profile": "reddit",
                "title": "Retry", "inputs": inputs, "log": [],
                "local_only": True, "est_chars": 50000,
                "video_group": "profile",
            }
            return retry_id

        with patch.object(app, "_enqueue_job", side_effect=queue_retry), \
                patch.object(app, "_preempt_for", return_value=True) as preempt, \
                patch.object(app.config, "max_concurrent_jobs", return_value=1):
            response = self.client.post(
                "/api/render",
                json={"resume_job": retry_id, "profile": "reddit",
                      "local_only": True},
            )
        self.assertEqual(response.status_code, 200)
        preempt.assert_called_once_with(retry_id)


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
                json={"localized_title": "Change This Habit Tonight",
                      "main_text": "Change Your Mind", "red_word": "Change",
                      "yellow_word": "Mind", "name": "Joseph Murphy"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.job_id]["status"], "done")
        self.assertEqual(app.JOBS[self.job_id]["publication_title"],
                         "Change This Habit Tonight")
        self.assertEqual(app.JOBS[self.job_id]["thumbnail"]["state"],
                         "generating_image")

    def test_word_package_uses_profile_language_and_trigger_guide(self):
        app.JOBS[self.job_id].update(
            profile="matthias-richter", inputs={"language": "german"},
            title="Say this before sleep - Joseph Murphy",
            thumbnail={"state": "generating_words"})
        package = {
            "localized_title": "Sagen Sie dies vor dem Schlafen – Joseph Murphy",
            "main_text": "Ihr Leben ändert sich",
            "red_word": "HEUTE NACHT", "yellow_word": "NEU BEGINNEN",
            "name": "Joseph Murphy",
        }
        with patch.object(app.llm, "chat_json", return_value=package) as mocked, \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(self.job_id)
        prompt = mocked.call_args.args[0]
        self.assertIn("Target language: German", prompt)
        self.assertIn("MUST use different wording", prompt)
        self.assertIn("Silently draft at least six", prompt)
        self.assertIn("English is forbidden", prompt)
        state = app.JOBS[self.job_id]["thumbnail"]
        self.assertEqual(state["language"], "German")
        self.assertEqual(state["localized_title"], package["localized_title"])

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

    def test_astrology_word_package_forces_empty_bottom_name(self):
        app.JOBS[self.job_id].update(
            profile="astrology", inputs={"language": "English"},
            title="A New Moon Pattern", thumbnail={"state": "generating_words"})
        package = {
            "localized_title": "Terra Tried Another Title",
            "main_text": "Notice This Pattern", "red_word": "NEW MOON",
            "yellow_word": "INNER CYCLE", "name": "ASTROLOGY",
        }
        with patch.object(app.llm, "chat_json", return_value=package) as mocked, \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(self.job_id)
        state = app.JOBS[self.job_id]["thumbnail"]
        self.assertEqual(state["state"], "words_ready")
        self.assertEqual(state["words"]["name"], "")
        self.assertIn("no bottom name label", mocked.call_args.args[0])

    def test_astrology_approval_allows_empty_bottom_name(self):
        app.JOBS[self.job_id].update(
            profile="astrology", inputs={"language": "English"},
            title="A New Moon Pattern")
        with patch.object(app, "_persist_queue"), \
                patch.object(app.threading, "Thread"):
            response = self.client.post(
                f"/api/thumbnail/job/{self.job_id}/words/approve",
                json={"localized_title": "A New Moon Pattern",
                      "main_text": "Notice This Pattern",
                      "red_word": "NEW MOON", "yellow_word": "INNER CYCLE",
                      "name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.JOBS[self.job_id]["thumbnail"]["words"]["name"], "")


class ThumbnailProviderTests(unittest.TestCase):
    def test_hidden_name_prompt_removes_bottom_label(self):
        prompt = thumbnail.build_prompt(
            "Main", "Red", "Gold", "ASTROLOGY",
            no_character=True, show_name=False)
        self.assertIn("Remove the name or label at the bottom completely", prompt)
        self.assertNotIn('bottom will be "ASTROLOGY"', prompt)

    def test_thumbnail_task_falls_back_to_main_key_on_auth_failure(self):
        with patch.object(thumbnail.config, "KIE_THUMBNAIL_API_KEY", "bad-key"), \
                patch.object(thumbnail.config, "KIE_API_KEY", "main-key"), \
                patch.object(thumbnail.clients, "kie_upload",
                             side_effect=["https://one", "https://two"]), \
                patch.object(thumbnail.clients, "kie_create_task",
                             side_effect=[RuntimeError("Unauthorized"), "task-2"]) as create, \
                patch.object(thumbnail.clients, "kie_poll",
                             return_value=["https://result"]), \
                patch.object(thumbnail.clients, "download",
                             return_value=Path("out.png")):
            result = thumbnail.generate(
                Path("template.png"), Path("person.png"), Path("out.png"),
                main_text="Main", red_word="Red", yellow_word="Gold",
                name="Name")
        self.assertEqual(result, Path("out.png"))
        self.assertEqual(create.call_args_list[0].kwargs["api_key"], "bad-key")
        self.assertEqual(create.call_args_list[1].kwargs["api_key"], "main-key")

    def test_thumbnail_uses_authorized_grok_image_fallback(self):
        with patch.object(thumbnail.config, "KIE_THUMBNAIL_API_KEY", "bad-key"), \
                patch.object(thumbnail.config, "KIE_API_KEY", "main-key"), \
                patch.object(thumbnail.config, "KIE_VIDEO_API_KEY", "video-key"), \
                patch.object(thumbnail.clients, "kie_upload",
                             side_effect=["https://one", "https://two"]), \
                patch.object(thumbnail.clients, "kie_create_task",
                             side_effect=[RuntimeError("Unauthorized"),
                                          RuntimeError("not authorized"),
                                          "task-3"]) as create, \
                patch.object(thumbnail.clients, "kie_poll",
                             return_value=["https://result"]), \
                patch.object(thumbnail.clients, "download",
                             return_value=Path("out.png")):
            thumbnail.generate(
                Path("template.png"), Path("person.png"), Path("out.png"),
                main_text="Main", red_word="Red", yellow_word="Gold",
                name="Name")
        third = create.call_args_list[2]
        self.assertEqual(third.args[0], "grok-imagine/image-to-image")
        self.assertEqual(third.kwargs["api_key"], "video-key")
        self.assertEqual(third.args[1]["image_urls"],
                         ["https://one", "https://two"])

    def test_thumbnail_uses_local_fallback_when_all_remote_keys_fail(self):
        with patch.object(thumbnail.config, "KIE_THUMBNAIL_API_KEY", "bad-key"), \
                patch.object(thumbnail.config, "KIE_API_KEY", "main-key"), \
                patch.object(thumbnail.config, "KIE_VIDEO_API_KEY", "video-key"), \
                patch.object(thumbnail.clients, "kie_upload",
                             side_effect=["https://one", "https://two"]), \
                patch.object(thumbnail.clients, "kie_create_task",
                             side_effect=RuntimeError("not authorized")) as create, \
                patch.object(thumbnail, "generate_local",
                             return_value=Path("out.png")) as local:
            result = thumbnail.generate(
                Path("template.png"), Path("person.png"), Path("out.png"),
                main_text="Ein Geheimnis verändert alles",
                red_word="Heute Nacht", yellow_word="Neues Leben",
                name="Joseph Murphy")
        self.assertEqual(result, Path("out.png"))
        self.assertEqual(create.call_count, 3)
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
