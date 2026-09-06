from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pipeline import artifacts, cancel, config, voiceover
from tests.isolation import isolated_runtime


def setUpModule():
    isolation = isolated_runtime()
    isolation.__enter__()
    unittest.addModuleCleanup(isolation.__exit__, None, None, None)


class VoiceCacheSafetyTests(unittest.TestCase):
    def test_slice_retries_local_validation_without_resubmitting_tts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcm, dest = root / "master.wav", root / "vo_001.mp3"
            pcm.write_bytes(b"pcm")

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"encoded audio" * 100)

            with patch.object(voiceover.render, "_run", side_effect=fake_run) as run, \
                    patch.object(voiceover.render, "is_valid_audio", side_effect=[False, True]):
                voiceover._write_slice(pcm, dest, 44.918, 88.987, 1)

            self.assertTrue(dest.exists())
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertIn("-t", command)
            self.assertNotIn("-to", command)
            self.assertEqual(list(root.glob(".*.slice.mp3")), [])

    def test_slice_accepts_a_full_independent_decode_after_probe_hiccup(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcm, dest = root / "master.wav", root / "vo_001.mp3"
            pcm.write_bytes(b"pcm")

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"encoded audio" * 100)

            with patch.object(voiceover.render, "_run", side_effect=fake_run) as run, \
                    patch.object(voiceover.render, "is_valid_audio", return_value=False), \
                    patch.object(voiceover.subprocess, "run",
                                 return_value=SimpleNamespace(returncode=0)) as decode:
                voiceover._write_slice(pcm, dest, 44.918, 88.987, 1)

            self.assertTrue(dest.exists())
            run.assert_called_once()
            decode.assert_called_once()

    def test_failed_slice_discards_ephemeral_pcm(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "tts_raw_000.mp3"
            raw.write_bytes(b"raw")

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"decoded")

            with patch.object(voiceover.render, "_run", side_effect=fake_run), \
                    patch.object(voiceover, "_slice_group_pcm", side_effect=RuntimeError("bad slice")):
                with self.assertRaisesRegex(RuntimeError, "bad slice"):
                    voiceover._slice_group([], [], raw, raw.with_suffix(".srt"), root)
            self.assertFalse(raw.with_suffix(".wav").exists())

    def setUp(self):
        cancel.set_current(None)

    def manifest(self, path, text):
        artifacts.publish(path.with_suffix(".manifest.json"), {
            "text": text, "voice": config.AI33_VOICE_ID, "speed": config.AI33_TTS_SPEED,
            "transcript": True, "endpoint": "/v3/text-to-speech"}, [path])

    def test_truncated_cached_raw_audio_is_rejected_on_every_retry(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            raw_audio = assets / "tts_raw_000.mp3"
            raw_audio.write_bytes(b"playable but incomplete narration")
            beats = [{"text": "x" * 1000}]
            self.manifest(raw_audio, beats[0]["text"])
            with patch.object(voiceover.render, "is_valid_audio",
                              side_effect=lambda path, **kwargs: path.exists()), \
                    patch.object(voiceover.render, "probe_duration", return_value=20), \
                    patch.object(voiceover.script, "CHARS_PER_SEC", 10), \
                    patch.object(voiceover, "generate_voiceover") as generate, \
                    patch.object(voiceover, "_slice_group") as split:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "truncated the request"):
                        voiceover.generate_voiceover_grouped(beats, [(0, [0])], assets)
                generate.assert_not_called()
                split.assert_not_called()
            self.assertTrue(raw_audio.exists())
            self.assertFalse((assets / "vo_000.mp3").exists())

    def test_complete_cached_raw_audio_is_reused_without_paid_generation(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            raw_audio = assets / "tts_raw_000.mp3"
            raw_audio.write_bytes(b"complete narration")
            beats = [{"text": "x" * 1000}]
            self.manifest(raw_audio, beats[0]["text"])
            with patch.object(voiceover.render, "is_valid_audio",
                              side_effect=lambda path, **kwargs: path.exists()), \
                    patch.object(voiceover.render, "probe_duration", return_value=95), \
                    patch.object(voiceover.script, "CHARS_PER_SEC", 10), \
                    patch.object(voiceover, "generate_voiceover") as generate, \
                    patch.object(voiceover, "_slice_group") as split:
                voiceover.generate_voiceover_grouped(beats, [(0, [0])], assets)
                generate.assert_not_called()
                split.assert_called_once_with(
                    beats, [0], raw_audio, raw_audio.with_suffix(".srt"), assets)

    def test_newly_generated_raw_audio_retains_truncation_guard(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            beats = [{"text": "x" * 1000}]
            with patch.object(voiceover.render, "is_valid_audio", return_value=False), \
                    patch.object(voiceover.render, "probe_duration", return_value=20), \
                    patch.object(voiceover.script, "CHARS_PER_SEC", 10), \
                    patch.object(voiceover, "generate_voiceover") as generate, \
                    patch.object(voiceover, "_slice_group") as split:
                with self.assertRaisesRegex(RuntimeError, "truncated the request"):
                    voiceover.generate_voiceover_grouped(beats, [(0, [0])], assets)
                generate.assert_called_once()
                split.assert_not_called()
