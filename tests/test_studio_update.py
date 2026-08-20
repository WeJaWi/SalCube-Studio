from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import app
from docx import Document
from pipeline import (clients, config, delivery_doc, identity, llm, operations,
                      orchestrator, render, research, script, storage, thumbnail,
                      voiceover)


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
            self.assertGreaterEqual(inputs["length_chars"], 170000)
            self.assertLessEqual(inputs["length_chars"], 195000)
        self.assertEqual(inputs["video_group"], "big")

    def test_geistmeister_spotify_style_is_persisted(self):
        inputs = app._build_inputs({
            "video_group": "small", "spotify_popup": True,
            "spotify_popup_style": "geistmeister",
        }, "A title")
        self.assertTrue(inputs["spotify_popup"])
        self.assertEqual(inputs["spotify_popup_style"], "geistmeister")


class AI33FallbackTests(unittest.TestCase):
    @staticmethod
    def response(status: int, data: dict):
        text = json.dumps(data)
        return SimpleNamespace(status_code=status, headers={}, text=text,
                               json=lambda: data)

    def setUp(self):
        self.mode_patch = patch.object(clients, "ai33_key_mode",
                                       return_value="auto")
        self.mode_patch.start()
        with clients._AI33_KEY_LOCK:
            clients._AI33_TASK_KEYS.clear()
            clients._AI33_PRIMARY_DEPLETED = False

    def tearDown(self):
        with clients._AI33_KEY_LOCK:
            clients._AI33_TASK_KEYS.clear()
            clients._AI33_PRIMARY_DEPLETED = False
        self.mode_patch.stop()

    def test_credit_exhaustion_switches_and_pins_tasks_to_backup(self):
        exhausted = self.response(
            402, {"success": False, "message": "Insufficient credits"})
        first_ok = self.response(
            200, {"success": True, "task_id": "backup-task-1"})
        second_ok = self.response(
            200, {"success": True, "task_id": "backup-task-2"})
        done = self.response(200, {"success": True, "status": "done"})
        with patch.object(config, "AI33_API_KEY", "primary-test-key"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "backup-test-key"), \
                patch.object(clients.requests, "post",
                             side_effect=[exhausted, first_ok, second_ok]) as post, \
                patch.object(clients.requests, "get", return_value=done) as get:
            task_id = clients.ai33_submit_json("/tts", {"text": "hello"})
            second_id = clients.ai33_submit_json("/tts", {"text": "again"})
            result = clients.ai33_poll(task_id, interval=0)
        self.assertEqual(task_id, "backup-task-1")
        self.assertEqual(second_id, "backup-task-2")
        self.assertEqual(result["status"], "done")
        used = [call.kwargs["headers"]["xi-api-key"]
                for call in post.call_args_list]
        self.assertEqual(used, ["primary-test-key", "backup-test-key",
                                "backup-test-key"])
        self.assertEqual(get.call_args.kwargs["headers"]["xi-api-key"],
                         "backup-test-key")

    def test_invalid_primary_key_does_not_switch_to_backup(self):
        invalid = self.response(
            401, {"success": False, "message": "Invalid API key"})
        with patch.object(config, "AI33_API_KEY", "primary-test-key"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "backup-test-key"), \
                patch.object(clients.requests, "post", return_value=invalid) as post:
            with self.assertRaisesRegex(RuntimeError, "Invalid API key"):
                clients.ai33_submit_json("/tts", {"text": "hello"})
        post.assert_called_once()
        self.assertFalse(clients._AI33_PRIMARY_DEPLETED)

    def test_ordinary_rate_limit_does_not_switch_to_backup(self):
        limited = self.response(
            429, {"success": False, "message": "Too many requests"})

        def rate_limited(call):
            call()
            raise RuntimeError("HTTP 429: Too many requests")

        with patch.object(config, "AI33_API_KEY", "primary-test-key"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "backup-test-key"), \
                patch.object(clients, "_retrying", side_effect=rate_limited), \
                patch.object(clients.requests, "post", return_value=limited) as post:
            with self.assertRaisesRegex(RuntimeError, "Too many requests"):
                clients.ai33_submit_json("/tts", {"text": "hello"})
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["xi-api-key"],
                         "primary-test-key")
        self.assertFalse(clients._AI33_PRIMARY_DEPLETED)

    def test_wrapped_credit_code_also_uses_backup(self):
        wrapped = self.response(
            200, {"success": False, "code": 402,
                  "message": "Account balance is insufficient"})
        ok = self.response(200, {"success": True, "task_id": "backup-task"})
        with patch.object(config, "AI33_API_KEY", "primary-test-key"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "backup-test-key"), \
                patch.object(clients.requests, "post",
                             side_effect=[wrapped, ok]) as post:
            task_id = clients.ai33_submit_form("/tts", {"text": "hello"})
        self.assertEqual(task_id, "backup-task")
        self.assertEqual(len(post.call_args_list), 2)

    def test_forced_backup_is_first_without_exposing_key_values(self):
        self.mode_patch.stop()
        self.mode_patch = patch.object(clients, "ai33_key_mode",
                                       return_value="backup")
        self.mode_patch.start()
        with patch.object(config, "AI33_API_KEY", "primary-test-key"), \
                patch.object(config, "AI33_API_KEY_BACKUP", "backup-test-key"):
            self.assertEqual(clients._ai33_keys(),
                             ["backup-test-key", "primary-test-key"])
            status = clients.ai33_key_status()
        self.assertEqual(status["active"], "backup")
        self.assertNotIn("key", status)


class VoiceoverCacheTests(unittest.TestCase):
    def test_invalid_cached_beat_is_resliced_from_valid_raw_audio(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "tts_raw_000.mp3"
            raw.write_bytes(b"raw" * 200)
            for i in range(2):
                (root / f"vo_{i:03d}.mp3").write_bytes(b"beat" * 100)
            beats = [{"text": "first narration"}, {"text": "second narration"}]

            def valid(path, min_duration=0.04):
                del min_duration
                return Path(path).name != "vo_001.mp3"

            with patch.object(render, "is_valid_audio", side_effect=valid), \
                    patch.object(voiceover, "_slice_group") as sliced:
                voiceover.generate_voiceover_grouped(
                    beats, [(0, [0, 1])], root)
            sliced.assert_called_once()

    def test_collapsed_final_cue_uses_text_proportional_boundary(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "tts_raw_000.mp3"
            raw.write_bytes(b"raw" * 200)
            (root / "tts_raw_000.srt").write_text("placeholder")
            beats = [{"text": "aaaa"}, {"text": "bbbb"}]

            def fake_run(cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b"audio" * 100)

            cues = [
                SimpleNamespace(start=0.0, end=10.0, text="aaaa"),
                SimpleNamespace(start=10.0, end=10.0, text="bbbb"),
            ]
            with patch.object(render, "_run", side_effect=fake_run), \
                    patch.object(render, "probe_duration", return_value=10.0), \
                    patch.object(render, "is_valid_audio", return_value=True), \
                    patch.object(voiceover.captions, "parse_srt",
                                 return_value=cues):
                voiceover._slice_group(
                    beats, [0, 1], raw, raw.with_suffix(".srt"), root)

            self.assertGreater(beats[1]["duration"], 4.0)
            self.assertTrue((root / "vo_001.mp3").exists())

    def test_header_only_narration_is_not_a_valid_audio_cache(self):
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "vo_102.mp3"
            audio.write_bytes(b"header" * 60)
            with patch.object(render, "probe_stream_duration", return_value=0):
                self.assertFalse(render.is_valid_audio(audio))

    def test_render_rejects_invalid_narration_before_ffmpeg(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "vo_102.mp3"
            audio.write_bytes(b"header" * 60)
            beat = {"kind": "blank", "duration": 12.0, "audio": audio}
            with patch.object(render, "is_valid_audio", return_value=False), \
                    patch.object(render, "_run") as ffmpeg:
                with self.assertRaisesRegex(
                        RuntimeError, "invalid narration audio asset"):
                    render.render_beat(
                        beat, root / "beat_102.mp4", 320, 180, 30, False)
            ffmpeg.assert_not_called()


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
        self.assertEqual(job["thumbnail"]["state"], "disabled")
        self.assertEqual(job["inputs"]["language"], "English")
        self.assertGreaterEqual(job["inputs"]["length_chars"], 40000)
        self.assertLessEqual(job["inputs"]["length_chars"], 70000)
        self.assertEqual(job["video_group"], "profile")
        research_start.assert_not_called()
        thumbnail_start.assert_not_called()

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
        self.assertEqual(job["thumbnail"]["state"], "disabled")
        thumbnail_start.assert_not_called()

    def test_direct_astrology_thumbnail_task_is_disabled(self):
        job_id = "55555555555555555555"
        submitted = "Your Moon Sign and the Patterns You Repeat"
        app.JOBS[job_id] = {
            "status": "queued", "step": "queued", "title": submitted,
            "profile": "astrology", "inputs": {"language": "English"},
            "log": [], "thumbnail": {"state": "generating_words"},
        }
        with patch.object(app.llm, "chat_json") as mocked, \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(job_id)
        self.assertEqual(app.JOBS[job_id]["thumbnail"], {"state": "disabled"})
        mocked.assert_not_called()

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
        self.assertEqual(app.JOBS[job_id]["thumbnail"]["state"], "disabled")
        saved = json.loads((job_dir / "inputs.json").read_text())
        self.assertEqual(saved["title"], "Title Two")
        thumb.assert_not_called()
        kick.assert_called_once()

    def test_direct_reddit_thumbnail_task_is_disabled(self):
        job_id = "33333333333333333333"
        app.JOBS[job_id] = {
            "status": "queued", "step": "queued", "title": "Chosen Title",
            "publication_title": "Chosen Title", "profile": "reddit",
            "inputs": {"language": "English"}, "log": [],
            "thumbnail": {"state": "generating_words"},
        }
        with patch.object(app.llm, "chat_json") as mocked, \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(job_id)
        self.assertEqual(app.JOBS[job_id]["thumbnail"], {"state": "disabled"})
        mocked.assert_not_called()

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

    def test_car_profile_skips_title_research_and_keeps_submitted_title(self):
        submitted = "Why Turbo Lag Still Matters"
        inputs = app._build_inputs({"video_group": "small"}, submitted)
        with patch.object(app.identity, "numeric_id",
                          return_value="66666666666666666666"), \
                patch.object(app, "_start_profile_research") as research_start, \
                patch.object(app, "_start_thumbnail_words") as thumbnail_start, \
                patch.object(app, "_persist_queue"):
            job_id = app._enqueue_job(inputs, "cars-modern")
        job = app.JOBS[job_id]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["title"], submitted)
        self.assertEqual(job["publication_title"], submitted)
        self.assertEqual(job["title_options"], {})
        self.assertEqual(job["research_attempt"], 0)
        self.assertEqual(job["thumbnail"], {"state": "disabled"})
        self.assertEqual(job["inputs"]["language"], "English")
        self.assertGreaterEqual(job["inputs"]["length_chars"], 32000)
        self.assertLessEqual(job["inputs"]["length_chars"], 47000)
        research_start.assert_not_called()
        thumbnail_start.assert_not_called()

    def test_all_car_profiles_have_distinct_automotive_accuracy_rules(self):
        for name in ("cars", "carparts", "cars-modern", "cars-old"):
            profile = orchestrator.load_profile(name)
            self.assertFalse(profile["research"]["enabled"])
            self.assertTrue(profile["title"]["lock_submitted"])
            self.assertFalse(profile["thumbnail"]["enabled"])
            self.assertEqual(profile["defaults"]["language"], "English")
            self.assertEqual(profile["defaults"]["length_chars_min"], 32000)
            self.assertEqual(profile["defaults"]["length_chars_max"], 47000)
            rules = " ".join(profile["rules"]).lower()
            self.assertIn("never invent", rules)
        self.assertIn("torque", " ".join(
            orchestrator.load_profile("carparts")["rules"]).lower())
        self.assertIn("software", " ".join(
            orchestrator.load_profile("cars-modern")["rules"]).lower())
        self.assertIn("period", " ".join(
            orchestrator.load_profile("cars-old")["rules"]).lower())

    def test_automotive_research_requires_precise_sourced_claims(self):
        response = {
            "research_brief": "Verified model-year and market distinctions. " * 30,
            "source_material": "Manufacturer workshop manual: https://example.test/manual",
            "titles": ["Title One", "Title Two", "Title Three"],
        }
        with patch.object(research.llm, "chat_json", return_value=response) as mocked:
            package = research.automotive_package("carparts", "water pump failure")
        prompt = mocked.call_args.args[0]
        self.assertIn("torque values", prompt)
        self.assertIn("factory service information", prompt)
        self.assertEqual(package["titles"], response["titles"])
        self.assertEqual(package["source_metadata"]["profile"], "carparts")

    def test_duplicate_generated_title_is_rejected_before_queueing(self):
        current_id = "77777777777777777777"
        existing_id = "88888888888888888888"
        current_dir = self.out / current_id
        current_dir.mkdir()
        candidates = ["Same Car Title", "Alternative Two", "Alternative Three"]
        app.JOBS[existing_id] = {
            "status": "done", "profile": "astrology-search", "title": "Same Car Title",
            "publication_title": "Same Car Title", "title_options": {"state": "approved"},
        }
        app.JOBS[current_id] = {
            "status": "awaiting_title", "step": "title", "title": "car topic",
            "profile": "astrology-search", "inputs": {"title": "car topic"}, "log": [],
            "title_options": {"state": "ready", "theme": "car topic",
                              "candidates": candidates},
            "thumbnail": {"state": "disabled"},
        }
        fake_drive = SimpleNamespace(configured=False, authorized=False)
        with patch.object(app, "DRIVE", fake_drive), \
                patch.object(app, "_persist_queue"), \
                patch.object(app, "_kick_queue") as kick:
            response = self.client.post(
                f"/api/title/job/{current_id}/approve",
                json={"title": "Same Car Title"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("naming error", response.get_json()["error"])
        self.assertEqual(app.JOBS[current_id]["status"], "awaiting_title")
        kick.assert_not_called()


class DeliveryDocumentTests(unittest.TestCase):
    def test_docx_contains_only_unique_titles_and_no_thumbnails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = delivery_doc.create(
                ["First Finished Video", "Second Finished Video",
                 "first finished video"], root / delivery_doc.DOCX_NAME)
            doc = Document(out)
            self.assertEqual([p.text for p in doc.paragraphs],
                             ["First Finished Video", "Second Finished Video"])
            self.assertEqual(len(doc.inline_shapes), 0)
            self.assertEqual(delivery_doc.read_titles(out),
                             ["First Finished Video", "Second Finished Video"])
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

    def test_drive_update_session_uses_patch_and_preserves_parent(self):
        drive = storage.GoogleDriveStorage()
        response = type("Response", (), {
            "status_code": 200, "headers": {"Location": "https://session"},
        })()
        with patch.object(drive, "_request", return_value=response) as request_call:
            drive._start_session(
                delivery_doc.DOCX_NAME, 4321, "profile-folder",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                file_id="existing-doc")
        self.assertEqual(request_call.call_args.args[:2],
                         ("PATCH", f"{storage.DRIVE_UPLOAD_API}/files/existing-doc"))
        self.assertEqual(request_call.call_args.kwargs["json"],
                         {"name": delivery_doc.DOCX_NAME})

    def test_reauthorization_reuses_existing_app_root_folder(self):
        with TemporaryDirectory() as tmp:
            drive = storage.GoogleDriveStorage(
                client_file=Path(tmp) / "client.json",
                token_file=Path(tmp) / "token.json")
            with patch.object(drive, "find_file",
                              return_value={"id": "existing-root"}) as find, \
                    patch.object(drive, "_request") as request_call:
                folder_id = drive.ensure_folder()
            self.assertEqual(folder_id, "existing-root")
            find.assert_called_once_with(
                drive.folder_name, "root", mime_type=storage.FOLDER_MIME)
            request_call.assert_not_called()

    def test_disconnect_revokes_and_removes_local_token(self):
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.json"
            token_file.write_text(json.dumps({"refresh_token": "secret"}))
            drive = storage.GoogleDriveStorage(
                client_file=Path(tmp) / "client.json", token_file=token_file)
            response = SimpleNamespace(status_code=200)
            with patch.object(storage.requests, "post", return_value=response) as post:
                revoked = drive.disconnect()
            self.assertTrue(revoked)
            self.assertFalse(token_file.exists())
            self.assertFalse(drive.authorized)
            self.assertEqual(post.call_args.args[0], storage.OAUTH_REVOKE_URL)

    def test_finished_titles_are_merged_into_one_profile_document(self):
        class FakeDrive:
            configured = True
            authorized = True

            def __init__(self):
                self.remote = None
                self.uploads = []

            def ensure_profile_folder(self, profile):
                self.profile = profile
                return "folder-id"

            def find_file(self, name, folder_id):
                return self.remote

            def download_to_path(self, file_id, dest):
                return dest

            def upload(self, source, **kwargs):
                self.uploads.append(kwargs)
                self.remote = {"id": "doc-id", "name": kwargs["dest_name"]}
                return {"id": "doc-id", "name": kwargs["dest_name"],
                        "size": source.stat().st_size}

        fake = FakeDrive()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = (("90111111111111111111", "First Car Video"),
                    ("90222222222222222222", "Second Car Video"))
            for job_id, title in jobs:
                app.JOBS[job_id] = {
                    "status": "done", "profile": "cars", "title": title,
                    "publication_title": title, "drive": {
                        "state": "uploaded",
                        "files": {"final": {"id": f"video-{job_id}"}}},
                    "package": {"state": "waiting_for_video"}, "log": [],
                }
            with patch.object(app.config, "OUTPUT_DIR", root), \
                    patch.object(app, "DRIVE", fake), \
                    patch.object(app, "_persist_queue"):
                for job_id, _ in jobs:
                    app._upload_profile_docx(job_id, root / job_id)
            doc_path = (root / "profile-documents" / "cars" /
                        delivery_doc.DOCX_NAME)
            self.assertEqual(delivery_doc.read_titles(doc_path),
                             ["First Car Video", "Second Car Video"])
            self.assertEqual(fake.profile, "cars")
            self.assertEqual(fake.uploads[0]["file_id"], None)
            self.assertEqual(fake.uploads[1]["file_id"], "doc-id")
            self.assertEqual(fake.uploads[1]["folder_id"], "folder-id")

    def test_video_upload_uses_title_inside_profile_folder(self):
        class FakeDrive:
            configured = True
            authorized = True

            def __init__(self):
                self.upload_call = None

            def ensure_profile_folder(self, profile):
                self.profile = profile
                return "cars-folder"

            def find_file(self, name, folder_id):
                return None

            def upload(self, source, **kwargs):
                self.upload_call = kwargs
                return {"id": "video-id", "name": kwargs["dest_name"],
                        "size": source.stat().st_size}

        fake = FakeDrive()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_id = "90333333333333333333"
            job_dir = root / job_id
            job_dir.mkdir()
            video = job_dir / "final.mp4"
            video.write_bytes(b"not-a-real-video")
            app.JOBS[job_id] = {
                "status": "uploading", "profile": "cars-modern",
                "title": "How Modern Cars See the Road", "log": [],
                "drive": {"state": "uploading", "files": {}},
            }
            with patch.object(app, "DRIVE", fake), \
                    patch.object(app, "_drive_deliverables",
                                 return_value=[("final", video)]), \
                    patch.object(app, "_upload_profile_docx") as docx_update, \
                    patch.object(app, "_persist_queue"):
                app._upload_job_deliverables(job_id, job_dir)
            self.assertEqual(fake.profile, "cars-modern")
            self.assertEqual(fake.upload_call["folder_id"], "cars-folder")
            self.assertEqual(fake.upload_call["dest_name"],
                             "How Modern Cars See the Road.mp4")
            docx_update.assert_called_once_with(job_id, job_dir)


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

    def test_all_encoders_use_gapless_segment_timing(self):
        args = render._encode_args(quality="veryfast", crf=20)
        self.assertEqual(args[args.index("-bf") + 1], "0")
        self.assertEqual(
            args[args.index("-video_track_timescale") + 1], "15360")

    def test_vidforge_final_preset_matches_accepted_videotoolbox_export(self):
        with patch.object(render.config, "gpu_encoder",
                          return_value="h264_videotoolbox"):
            args = render._vidforge_final_encode_args(gpu=True)
        self.assertEqual(args[:2], ["-c:v", "h264_videotoolbox"])
        self.assertEqual(args[args.index("-b:v") + 1], "10M")
        self.assertEqual(args[args.index("-maxrate") + 1], "14M")
        self.assertEqual(args[args.index("-bf") + 1], "0")
        self.assertEqual(
            args[args.index("-video_track_timescale") + 1], "15360")

    def test_hardware_probe_skips_a_compiled_but_broken_encoder(self):
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return SimpleNamespace(
                returncode=0 if "h264_qsv" in cmd else 1)

        with patch.object(config, "_gpu_encoder_cache", None), \
                patch.object(config.sys, "platform", "win32"), \
                patch.object(config.subprocess, "run", side_effect=fake_run):
            self.assertEqual(config.gpu_encoder(), "h264_qsv")
        self.assertIn("h264_nvenc", calls[0])
        self.assertIn("h264_amf", calls[1])
        self.assertIn("h264_qsv", calls[2])

    def test_full_final_pass_rebuilds_pcm_and_normalizes_spotify_mp4(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "final.mp4"
            video.write_bytes(b"stitched")
            commands = []

            def fake_ffmpeg(cmd, **_kwargs):
                commands.append(cmd)
                Path(cmd[-1]).write_bytes(b"encoded")

            contract = {
                "video": {"width": 1920, "height": 1080,
                          "pix_fmt": "yuv420p", "profile": "High"},
                "audio": {"profile": "LC"},
            }
            with patch.object(render, "probe_stream_duration",
                              return_value=10.0), \
                    patch.object(render, "_run", side_effect=fake_ffmpeg), \
                    patch.object(render, "_validate_spotify_export",
                                 return_value=contract), \
                    patch.object(render.config, "gpu_encoder",
                                 return_value="h264_videotoolbox"):
                render.encode_final_export(video, root, fps=30, gpu=True)

            self.assertEqual(video.read_bytes(), b"encoded")
            self.assertEqual(len(commands), 2)
            self.assertIn("pcm_s16le", commands[0])
            final = commands[1]
            joined = " ".join(final)
            self.assertIn("fps=fps=30", joined)
            self.assertIn("format=yuv420p", joined)
            self.assertIn("setpts=N/(30*TB)", joined)
            self.assertIn("-profile:v high", joined)
            self.assertIn("-b:v 10M", joined)
            self.assertIn("-c:a aac -b:a 192k", joined)
            self.assertIn("-movflags +faststart", joined)

    def test_spotify_validation_rejects_the_old_nonzero_video_start(self):
        old_contract = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264",
                 "profile": "High", "pix_fmt": "yuv420p",
                 "width": 1920, "height": 1080,
                 "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
                 "start_time": "0.085", "duration": "120.000"},
                {"codec_type": "audio", "codec_name": "aac",
                 "profile": "LC", "sample_rate": "44100", "channels": 2,
                 "start_time": "0.000", "duration": "120.000"},
            ],
            "format": {"nb_streams": 2, "duration": "120.000"},
        }
        with patch.object(render, "_probe_export_contract",
                          return_value=old_contract):
            with self.assertRaisesRegex(RuntimeError, "first video PTS"):
                render._validate_spotify_export(Path("old.mp4"), 30)

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
        self.assertEqual(model, "qwen/qwen3.8-27b")
        mocked.assert_called_once()

    def test_openrouter_monitor_payload_uses_qwen_reasoning(self):
        spec = {
            "format": "openai", "model": "qwen/qwen3.8-27b",
            "reasoning": True, "reasoning_max_tokens": 256,
        }
        payload = llm._payload(spec, "inspect this", 650)
        self.assertEqual(payload["model"], "qwen/qwen3.8-27b")
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

    def test_observer_switches_ai33_only_on_explicit_credit_exhaustion(self):
        with TemporaryDirectory() as tmp:
            center = operations.OperationsCenter(
                lambda: {}, interval=60)
            center.log_file = Path(tmp) / "ops.jsonl"
            snapshot = {"jobs": [{
                "video_id": "job-1", "status": "error",
                "error": "ai33: insufficient credits",
            }]}
            current = {"mode": "auto", "active": "primary",
                       "backup_configured": True,
                       "primary_configured": True,
                       "primary_depleted_this_process": False}
            changed = {**current, "mode": "backup", "active": "backup"}
            with patch.object(clients, "ai33_key_status",
                              side_effect=[current, changed]), \
                    patch.object(clients, "set_ai33_key_mode",
                                 return_value=changed) as switch:
                result = center._maybe_switch_ai33(snapshot)
        switch.assert_called_once()
        self.assertEqual(result["kind"], "action")
        self.assertEqual(result["ai33"]["active"], "backup")

    def test_observer_does_not_switch_on_ordinary_ai33_failure(self):
        center = operations.OperationsCenter(lambda: {}, interval=60)
        snapshot = {"jobs": [{
            "video_id": "job-1", "status": "error",
            "error": "ai33 network timeout",
        }]}
        with patch.object(clients, "set_ai33_key_mode") as switch:
            self.assertIsNone(center._maybe_switch_ai33(snapshot))
        switch.assert_not_called()


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

    def test_astrology_thumbnail_word_task_is_disabled(self):
        app.JOBS[self.job_id].update(
            profile="astrology", inputs={"language": "English"},
            title="A New Moon Pattern", thumbnail={"state": "generating_words"})
        with patch.object(app.llm, "chat_json") as mocked, \
                patch.object(app, "_persist_queue"):
            app._thumbnail_words_task(self.job_id)
        state = app.JOBS[self.job_id]["thumbnail"]
        self.assertEqual(state["state"], "disabled")
        mocked.assert_not_called()

    def test_astrology_thumbnail_approval_is_rejected(self):
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
        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.get_json()["error"])


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
