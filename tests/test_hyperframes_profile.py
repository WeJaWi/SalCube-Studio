from __future__ import annotations

import json
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

    def test_profile_validation_fails_closed(self):
        unsafe = json.loads(json.dumps(self.profile))
        unsafe["video"]["broll_mix"]["stock"] = 0.1
        with self.assertRaisesRegex(ValueError, "external visual"):
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
        call.assert_called_once()

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
        self.assertIn('data-composition-id="main"', source)
        self.assertIn("gsap.timeline({paused:true})", source)
        self.assertIn('window.__timelines["main"] = tl', source)
        self.assertIn("tl.from", source)
        self.assertIn("data-track-index=\"20\"", source)
        self.assertNotIn("Math.random", source)
        self.assertNotIn("Date.now", source)
        self.assertNotIn("repeat:-1", source)
        self.assertNotIn("<iframe", source)

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
