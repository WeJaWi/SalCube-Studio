import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from pipeline import orchestrator


class ResumeCacheTests(unittest.TestCase):
    def test_replan_invalidates_raw_narration_but_keeps_music(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            stale = ("tts_raw_000.mp3", "tts_raw_000.srt", "tts_raw_000.wav",
                     "vo_001.mp3", "vo_001.srt", "beat_001.mp4", "host_001.mp4")
            for name in stale + ("music.mp3", "reference.png"):
                (assets / name).write_bytes(b"cached")
            self.assertEqual(orchestrator._clear_replanned_assets(assets), len(stale))
            self.assertEqual({p.name for p in assets.iterdir()}, {"music.mp3", "reference.png"})

    def test_invalid_video_does_not_hide_valid_image_fallback(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "beat_001.mp4").write_bytes(b"bad" * 4000)
            image = assets / "beat_001_fb.png"
            Image.new("RGB", (32, 32)).save(image)
            self.assertEqual(orchestrator._cached_broll_asset(assets, 1), (image, "png", True))

    def test_html_image_is_not_a_resume_hit(self):
        with TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "beat_000.jpg").write_bytes(b"<html>error</html>" * 1000)
            self.assertIsNone(orchestrator._cached_broll_asset(assets, 0))

    def test_valid_video_is_preferred_and_checked_as_video(self):
        with TemporaryDirectory() as tmp, patch.object(orchestrator.media_cache, "valid_media", return_value=True) as validate:
            assets = Path(tmp)
            path = assets / "beat_002.mp4"
            self.assertEqual(orchestrator._cached_broll_asset(assets, 2), (path, "mp4", False))
            validate.assert_called_once_with(path, "video")
