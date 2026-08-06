from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import app
from pipeline import (identity, llm, operations, orchestrator, render, script,
                      thumbnail)


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
            profile="matthias-richter", inputs={},
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


class ThumbnailProviderTests(unittest.TestCase):
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
