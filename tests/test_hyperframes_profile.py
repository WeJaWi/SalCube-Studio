from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline import hyperframes, orchestrator


class HyperFramesProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads(
            (Path(__file__).parents[1] / "profiles" / "hyperframes.json").read_text())

    def test_profile_is_a_zero_external_visual_contract(self):
        hyperframes.validate_profile(self.profile)
        self.assertTrue(hyperframes.is_hyperframes_profile(self.profile))
        video = self.profile["video"]
        self.assertFalse(video["external_visuals_allowed"])
        self.assertEqual(video["broll_defaults"], [])
        self.assertTrue(all(
            video["broll_mix"][key] == 0
            for key in ("stock", "stock_image", "ai_video", "ai_image", "host")))
        self.assertFalse(self.profile["host"]["enabled"])
        self.assertFalse(self.profile["thumbnail"]["enabled"])

    def test_profile_validation_fails_closed(self):
        unsafe = json.loads(json.dumps(self.profile))
        unsafe["video"]["broll_mix"]["stock"] = 0.1
        with self.assertRaisesRegex(ValueError, "external visual"):
            hyperframes.validate_profile(unsafe)
        unsafe = json.loads(json.dumps(self.profile))
        unsafe["thumbnail"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "thumbnail"):
            hyperframes.validate_profile(unsafe)
        unsafe = json.loads(json.dumps(self.profile))
        unsafe["video"]["hyperframes"]["palette"]["accent"] = "red;}</style>"
        with self.assertRaisesRegex(ValueError, "palette.accent"):
            hyperframes.validate_profile(unsafe)
        unsafe = json.loads(json.dumps(self.profile))
        unsafe["video"]["fps"] = 25
        with self.assertRaisesRegex(ValueError, "fps"):
            hyperframes.validate_profile(unsafe)

    def test_runtime_preflight_rejects_old_node_before_paid_work(self):
        with patch.object(hyperframes, "doctor", return_value={
                "node": "/bin/node", "npx": "/bin/npx",
                "ffmpeg": "/bin/ffmpeg", "ffprobe": "/bin/ffprobe",
                "node_major": 21, "cli_version": hyperframes.CLI_VERSION}):
            with self.assertRaisesRegex(RuntimeError, "Node 22"):
                hyperframes.ensure_runtime()

    def test_authoring_budget_is_persisted_before_provider_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            design = hyperframes._design(self.profile, {})

            def authored(*_args, **_kwargs):
                self.assertTrue((root / "authoring-budget.json").exists())
                return {"scenes": []}

            with patch.object(hyperframes.llm, "chat_json", side_effect=authored) as call:
                hyperframes.build_blueprint(
                    [{"text": "A budgeted scene.", "duration": 4.0}],
                    design, self.profile, "gpt-5.6-luna", root,
                    lambda *_: None)
            budget = json.loads((root / "authoring-budget.json").read_text())
        call.assert_called_once()
        self.assertEqual(budget["version"], 2)
        self.assertEqual(budget["json_attempts"], 2)
        self.assertEqual(budget["requests"][0]["api_output_token_cap"], 10000)
        expected = ((budget["requests"][0]["input_token_cap"] * 500
                     + 10000 * 2000) * 2 / 1_000_000)
        self.assertAlmostEqual(budget["reserved_kie_credits"], expected, places=3)

    def test_authoring_ceiling_fails_before_provider_submission(self):
        profile = json.loads(json.dumps(self.profile))
        profile["video"]["hyperframes"]["authoring"]["max_kie_credits"] = 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hyperframes.llm, "chat_json") as call:
            with self.assertRaisesRegex(RuntimeError, "Kie ceiling"):
                hyperframes.build_blueprint(
                    [{"text": "An intentionally bounded scene.", "duration": 4.0}],
                    hyperframes._design(profile, {}), profile,
                    "gpt-5.6-luna", Path(tmp), lambda *_: None)
        call.assert_not_called()

    def test_semantic_scene_split_preserves_all_words(self):
        source = (
            "The first idea establishes the argument. "
            "The second idea explains why it matters. "
            "A final sentence turns that understanding into a practical choice."
        )
        parts = hyperframes.segment_narration(source, target_seconds=3,
                                              chars_per_second=12)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(source.split()))

    def test_caption_fallback_is_contiguous_readable_and_complete(self):
        source = ("A deterministic fallback keeps every spoken word visible "
                  "when the timing provider does not return a transcript.")
        cues = hyperframes._fallback_caption_segments(source, 8.0)
        self.assertGreater(len(cues), 1)
        self.assertEqual(cues[0].start, 0.0)
        self.assertEqual(cues[-1].end, 8.0)
        self.assertEqual(" ".join(c.text for c in cues), source)
        self.assertTrue(all(len(c.text) <= 55 for c in cues))
        self.assertAlmostEqual(hyperframes._caption_coverage(cues, 8.0), 1.0)

    def test_scene_capacity_fails_before_voice_generation(self):
        profile = json.loads(json.dumps(self.profile))
        profile["video"]["hyperframes"]["authoring"]["scenes_per_call"] = 4
        profile["video"]["hyperframes"]["authoring"]["max_calls"] = 1
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hyperframes, "segment_narration",
                             return_value=[f"scene {i}" for i in range(5)]), \
                patch.object(hyperframes.voiceover, "generate_voiceover_grouped") as tts:
            with self.assertRaisesRegex(RuntimeError, "before starting paid voice"):
                hyperframes.prepare_narration(
                    "too long", profile, "English", Path(tmp) / "hyperframes",
                    lambda *_: None)
        tts.assert_not_called()

    def test_short_authoring_chunk_cannot_shift_later_scenes(self):
        profile = json.loads(json.dumps(self.profile))
        profile["video"]["hyperframes"]["authoring"]["scenes_per_call"] = 4
        beats = [{"text": f"Narration {i}.", "duration": 4.0}
                 for i in range(1, 9)]
        responses = [
            {"scenes": [{"scene": 1, "title": "Authored 1"},
                        {"scene": 3, "title": "Authored 3"}]},
            {"scenes": [{"scene": i, "title": f"Authored {i}"}
                        for i in range(5, 9)]},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hyperframes.llm, "chat_json", side_effect=responses):
            blueprint = hyperframes.build_blueprint(
                beats, hyperframes._design(profile, {}), profile,
                "gpt-5.6-luna", Path(tmp), lambda *_: None)
        self.assertEqual(blueprint["scenes"][0]["title"], "Authored 1")
        self.assertNotEqual(blueprint["scenes"][1]["title"], "Authored 3")
        self.assertEqual(blueprint["scenes"][2]["title"], "Authored 3")
        self.assertEqual(blueprint["scenes"][4]["title"], "Authored 5")

    def test_blueprint_cache_contract_includes_render_and_runtime_inputs(self):
        beats = [{"text": "One.", "duration": 4.0}]
        design = hyperframes._design(self.profile, {})
        first = hyperframes._blueprint_contract(
            "One.", beats, design, self.profile, "gpt-5.6-luna")
        changed = json.loads(json.dumps(self.profile))
        changed["video"]["width"] = 1280
        second = hyperframes._blueprint_contract(
            "One.", beats, design, changed, "gpt-5.6-luna")
        self.assertNotEqual(hyperframes.artifacts.digest(first),
                            hyperframes.artifacts.digest(second))
        self.assertEqual(first["compiler_version"], hyperframes.COMPILER_VERSION)
        self.assertEqual(first["runtime_version"], hyperframes.CLI_VERSION)

    def test_composition_complexity_fails_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            blueprint = self._blueprint(Path(tmp))
        profile = json.loads(json.dumps(self.profile))
        profile["video"]["hyperframes"]["authoring"]["max_tweens"] = 100
        with self.assertRaisesRegex(RuntimeError, "max_tweens"):
            hyperframes.validate_composition_complexity(
                blueprint, cue_count=100, profile=profile)

    def _blueprint(self, root: Path):
        profile = json.loads(json.dumps(self.profile))
        profile["video"]["hyperframes"]["authoring"]["enabled"] = False
        beats = [
            {"text": "A system becomes understandable when its parts have clear roles.",
             "duration": 5.2},
            {"text": "Timing connects those parts into a coherent experience.",
             "duration": 4.8},
            {"text": "Validation turns that experience into a reliable product.",
             "duration": 5.0},
        ]
        design = hyperframes._design(profile, {})
        hyperframes.write_design(root, design)
        blueprint = hyperframes.build_blueprint(
            beats, design, profile, "unused", root, lambda *_: None)
        hyperframes.validate_blueprint(blueprint)
        return blueprint

    def test_fallback_blueprint_is_custom_deterministic_and_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._blueprint(Path(tmp))
            second = self._blueprint(Path(tmp))
        self.assertEqual(first, second)
        self.assertEqual(first["duration"], 15.0)
        self.assertEqual([s["start"] for s in first["scenes"]], [0.0, 5.2, 10.0])
        for scene in first["scenes"]:
            self.assertEqual(set(scene["entrances"]),
                             {"kicker", "title", "body", "accent"})
            self.assertGreaterEqual(
                len({entry["ease"] for entry in scene["entrances"].values()}), 3)

    def test_compiler_owns_executable_source_and_obeys_timeline_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blueprint = self._blueprint(root)
            narration = root / "assets" / "narration.wav"
            narration.parent.mkdir()
            narration.write_bytes(b"fixture")
            cues = []
            index = hyperframes.compile_project(root, blueprint, narration, cues)
            source = index.read_text()
            local_gsap_exists = (root / "vendor" / "gsap.min.js").exists()
            motion = json.loads((root / "index.motion.json").read_text())
        self.assertIn('data-composition-id="main"', source)
        self.assertIn("gsap.timeline({paused:true})", source)
        self.assertIn('window.__timelines["main"] = tl', source)
        self.assertIn("tl.set", source)
        self.assertIn("tl.to", source)
        self.assertIn("tl.fromTo", source)
        self.assertNotIn("tl.from(", source)
        self.assertIn("data-track-index=\"20\"", source)
        self.assertIn('http-equiv="Content-Security-Policy"', source)
        self.assertIn('src="vendor/gsap.min.js"', source)
        self.assertTrue(local_gsap_exists)
        self.assertEqual(motion["duration"], blueprint["duration"])
        self.assertTrue(any(a["kind"] == "appearsBy"
                            for a in motion["assertions"]))
        self.assertTrue(any(a["kind"] == "keepsMoving"
                            for a in motion["assertions"]))
        self.assertNotIn("https://", source)
        self.assertNotIn("Math.random", source)
        self.assertNotIn("Date.now", source)
        self.assertNotIn("repeat:-1", source)
        self.assertNotIn("<iframe", source)

    def test_chatty_command_cannot_deadlock_on_pipe_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, stderr = hyperframes._run_process([
                sys.executable, "-c",
                "import sys; sys.stdout.write('x'*2000000); sys.stderr.write('y'*2000000)",
            ], Path(tmp), timeout=15)
        self.assertEqual(len(stdout), 2_000_000)
        self.assertEqual(len(stderr), 2_000_000)

    def test_validation_fails_closed_on_malformed_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(hyperframes, "ensure_runtime"), \
                patch.object(hyperframes, "_run_process", return_value=("not json", "")):
            with self.assertRaisesRegex(RuntimeError, "JSON object"):
                hyperframes.check_project(Path(tmp))

    def test_validation_report_is_resumable(self):
        report = {"ok": True, "motion": {"enabled": True}}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "index.motion.json").write_text('{}')
            with patch.object(hyperframes, "ensure_runtime"), \
                    patch.object(hyperframes, "_run_process",
                                 return_value=(json.dumps(report), "")) as command:
                first = hyperframes.check_project(project)
                second = hyperframes.check_project(project)
        self.assertEqual(first, second)
        command.assert_called_once()

    def test_missing_validation_snapshot_invalidates_resume(self):
        report = {
            "ok": True,
            "snapshots": {"enabled": True, "files": ["snapshots/frame.png"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            def command(*_args, **_kwargs):
                snapshot = project / "snapshots" / "frame.png"
                snapshot.parent.mkdir(exist_ok=True)
                snapshot.write_bytes(b"png")
                return json.dumps(report), ""

            with patch.object(hyperframes, "ensure_runtime"), \
                    patch.object(hyperframes, "_run_process", side_effect=command) as run:
                hyperframes.check_project(project)
                (project / "snapshots" / "frame.png").unlink()
                hyperframes.check_project(project)
        self.assertEqual(run.call_count, 2)

    def test_render_master_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            def fake_render(_project, output, *_args, **_kwargs):
                output.write_bytes(b"video")
                return output

            with patch.object(hyperframes, "render_project", side_effect=fake_render) as command, \
                    patch.object(hyperframes.render, "is_valid_video", return_value=True):
                first = hyperframes.ensure_render_master(project, 30)
                second = hyperframes.ensure_render_master(project, 30)
        self.assertEqual(first, second)
        command.assert_called_once()

    def test_failed_final_export_preserves_last_verified_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            master = job / "master.mp4"
            final = job / "final.mp4"
            master.write_bytes(b"new master")
            final.write_bytes(b"known good")
            with patch.object(hyperframes, "_source_fingerprint",
                              return_value={"sha256": "new"}), \
                    patch.object(hyperframes.render, "encode_final_export",
                                 side_effect=RuntimeError("encode failed")):
                with self.assertRaisesRegex(RuntimeError, "encode failed"):
                    hyperframes.ensure_final_export(job, master, 30)
            self.assertEqual(final.read_bytes(), b"known good")
            self.assertFalse((job / ".final-next.mp4").exists())

    def test_verified_final_export_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            master = job / "master.mp4"
            output = job / "final.mp4"
            master.write_bytes(b"master")
            output.write_bytes(b"verified output")
            source = {"sha256": "fixed", "bytes": 6, "duration": 4.0}
            contract = hyperframes._final_export_contract(source, 30, False)
            hyperframes.artifacts.publish(
                output.with_suffix(".export.json"), contract, [output],
                hash_outputs=True)
            with patch.object(hyperframes, "_source_fingerprint", return_value=source), \
                    patch.object(hyperframes.render, "is_valid_video", return_value=True), \
                    patch.object(hyperframes.render, "encode_final_export") as encode:
                result, fingerprint = hyperframes.ensure_final_export(
                    job, master, 30)
        self.assertEqual(result, output)
        self.assertEqual(fingerprint, source)
        encode.assert_not_called()

    def test_delivery_receipts_satisfy_uploader_contract(self):
        import app as studio_app

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            project = job / "hyperframes"
            project.mkdir()
            blueprint = project / "video-blueprint.json"
            blueprint.write_text('{"schema_version":1}')
            output = job / "final.mp4"
            output.write_bytes(b"deterministic fixture")
            hyperframes.publish_delivery_receipts(
                job, project, blueprint, output, 30,
                hyperframes.artifacts.signature(output))
            with patch.object(studio_app.video_render, "is_valid_video", return_value=True):
                roles = studio_app._verified_delivery_roles(job, {})
        self.assertEqual([role for role, _path in roles], ["final"])

    def test_worker_capability_gate_is_fail_closed(self):
        import app as studio_app

        self.assertFalse(studio_app._worker_supports_profile({}, self.profile))
        worker = {"capabilities": {"hyperframes": {
            "ready": True,
            "engine_version": hyperframes.ENGINE_VERSION,
            "cli_version": hyperframes.CLI_VERSION,
        }}}
        self.assertTrue(studio_app._worker_supports_profile(worker, self.profile))

    def test_orchestrator_bypasses_every_legacy_visual_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            expected = job / "final.mp4"
            inputs = {
                "title": "A custom motion test",
                "language": "English",
                "length_chars": 500,
                "custom_script": "This is the complete approved narration.",
                "broll": ["stock", "ai_video"],
            }
            with patch.object(orchestrator.render, "ensure_ffmpeg"), \
                    patch.object(orchestrator, "_check_disk"), \
                    patch.object(orchestrator.hyperframes, "run", return_value=expected) as hf_run, \
                    patch.object(orchestrator.script, "plan_beats") as legacy_plan, \
                    patch.object(orchestrator.footage, "fetch_stock") as stock, \
                    patch.object(orchestrator.aivideo, "submit_clip") as ai_video:
                result = orchestrator.run_pipeline(
                    inputs, "hyperframes", job, profile_data=self.profile)
        self.assertEqual(result, expected)
        hf_run.assert_called_once()
        legacy_plan.assert_not_called()
        stock.assert_not_called()
        ai_video.assert_not_called()


if __name__ == "__main__":
    unittest.main()
