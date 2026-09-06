from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PIL import Image

from pipeline import cancel, config, footage, media_cache


class StockCacheTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in {
            "OUTPUT_DIR": self.root,
            "STOCK_CACHE_MAX_BYTES": 1024 * 1024,
            "STOCK_CACHE_TTL_SECONDS": 604800,
            "STOCK_SEARCH_CACHE_TTL_SECONDS": 86400,
            "STOCK_SEARCH_CACHE_MAX_ENTRIES": 16,
        }.items():
            patcher = patch.object(config, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        with media_cache._SEARCH_LOCK:
            media_cache._SEARCHES.clear()
        self.addCleanup(media_cache._SEARCHES.clear)
        self.addCleanup(cancel.set_current, None)
        self.source = self.root / "source.png"
        Image.new("RGB", (48, 32), "blue").save(self.source)

    @staticmethod
    def response(data):
        return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None,
                               close=Mock())

    def photo_download(self, _url, dest, **_kwargs):
        shutil.copyfile(self.source, dest)
        return dest

    def fetch_photo(self, dest, key="photo-1"):
        return media_cache.download("https://cdn.example/stock.jpg", dest,
                                    kind="photo", cache_key=key)

    def media_files(self):
        return list((self.root / ".stock-cache" / "media").glob("*.jpg"))

    def test_reuses_search_results_normalizes_query_and_closes_response(self):
        response = self.response({"hits": [{"id": 1}]})
        with patch.object(media_cache.requests, "get", return_value=response) as get:
            first = media_cache.search_json("https://provider.example/search",
                                            params={"q": " Blue   CAR "})
            first["hits"].clear()
            second = media_cache.search_json("https://provider.example/search",
                                             params={"q": "blue car"})
        self.assertEqual(second, {"hits": [{"id": 1}]})
        self.assertEqual(get.call_count, 1)
        response.close.assert_called_once()

    def test_search_survives_memory_clear_and_credentials_are_not_written(self):
        with patch.object(media_cache.requests, "get",
                          return_value=self.response({"hits": []})) as get:
            for _ in range(2):
                media_cache.search_json(footage.PIXABAY_VIDEO_API,
                                       params={"q": "car", "key": "secret-api-key"})
                media_cache._SEARCHES.clear()
        self.assertEqual(get.call_count, 1)
        saved = list((self.root / ".stock-cache" / "search").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertNotIn("secret-api-key", saved[0].name + saved[0].read_text())

    def test_search_key_separates_credentials(self):
        with patch.object(media_cache.requests, "get",
                          return_value=self.response({"hits": []})) as get:
            for key in ("first", "second"):
                media_cache.search_json(footage.PIXABAY_VIDEO_API,
                                       params={"q": "car", "key": key})
        self.assertEqual(get.call_count, 2)

    def test_search_cache_expires_and_is_bounded(self):
        with patch.object(config, "STOCK_SEARCH_CACHE_MAX_ENTRIES", 2), \
                patch.object(media_cache.requests, "get",
                             return_value=self.response({"hits": []})) as get:
            for query in ("a", "b", "c"):
                media_cache.search_json("https://provider.example/search", params={"q": query})
            self.assertEqual(len(media_cache._SEARCHES), 2)
            self.assertEqual(len(list((self.root / ".stock-cache/search").glob("*.json"))), 2)
            with patch.object(media_cache.time, "time", return_value=time.time() + 86401):
                media_cache.search_json("https://provider.example/search", params={"q": "c"})
        self.assertEqual(get.call_count, 4)

    def test_pixabay_has_minimum_one_day_ttl(self):
        with patch.object(config, "STOCK_SEARCH_CACHE_TTL_SECONDS", 1), \
                patch.object(media_cache.requests, "get",
                             return_value=self.response({"hits": []})) as get:
            media_cache.search_json(footage.PIXABAY_VIDEO_API, params={"q": "car"})
            with patch.object(media_cache.time, "time", return_value=time.time() + 3600):
                media_cache.search_json(footage.PIXABAY_VIDEO_API, params={"q": "car"})
        self.assertEqual(get.call_count, 1)

    def test_failed_search_is_not_cached(self):
        bad = self.response({})
        bad.raise_for_status = Mock(side_effect=requests.HTTPError("bad response"))
        with patch.object(media_cache.requests, "get",
                          side_effect=[bad, self.response({"hits": []})]) as get:
            with self.assertRaises(requests.HTTPError):
                media_cache.search_json("https://provider.example/search", params={"q": "car"})
            media_cache.search_json("https://provider.example/search", params={"q": "car"})
        self.assertEqual(get.call_count, 2)
        bad.close.assert_called_once()

    def test_simultaneous_searches_make_one_request(self):
        gate = threading.Barrier(4)
        def request():
            gate.wait(timeout=5)
            return media_cache.search_json("https://provider.example/search", params={"q": "car"})
        with patch.object(media_cache.requests, "get",
                          return_value=self.response({"hits": []})) as get:
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: request(), range(4)))
        self.assertEqual(get.call_count, 1)

    def test_simultaneous_downloads_use_one_original_and_independent_copies(self):
        gate = threading.Barrier(4)
        def request(index):
            gate.wait(timeout=5)
            return self.fetch_photo(self.root / f"job-{index}" / "photo.jpg")
        with patch.object(media_cache.clients, "download", side_effect=self.photo_download) as dl:
            with ThreadPoolExecutor(max_workers=4) as executor:
                paths = list(executor.map(request, range(4)))
        self.assertEqual(dl.call_count, 1)
        self.assertTrue(all(path.read_bytes() == self.source.read_bytes() for path in paths))
        paths[0].write_bytes(b"modified job file")
        self.assertTrue(media_cache.valid_media(self.media_files()[0], "photo"))
        self.assertTrue(media_cache.valid_media(paths[1], "photo"))

    def test_corrupt_cached_original_is_downloaded_again(self):
        with patch.object(media_cache.clients, "download", side_effect=self.photo_download) as dl:
            self.fetch_photo(self.root / "first.jpg")
            self.media_files()[0].write_text("<html>server error</html>")
            second = self.fetch_photo(self.root / "second.jpg")
        self.assertEqual(dl.call_count, 2)
        self.assertTrue(media_cache.valid_media(second, "photo"))

    def test_invalid_new_download_does_not_replace_existing_job_file(self):
        dest = self.root / "photo.jpg"
        dest.write_bytes(b"existing file")
        def broken(_url, path, **_kwargs):
            path.write_text("<html>server error</html>")
        with patch.object(media_cache.clients, "download", side_effect=broken):
            with self.assertRaisesRegex(ValueError, "not a valid photo"):
                self.fetch_photo(dest)
        self.assertEqual(dest.read_bytes(), b"existing file")
        self.assertEqual(self.media_files(), [])
        self.assertEqual(list(self.root.rglob(".stock-*.jpg")), [])

    def test_interrupted_download_leaves_no_partial_cache_or_job_file(self):
        def broken(_url, path, **_kwargs):
            path.write_bytes(b"partial")
            raise requests.ConnectionError("interrupted")
        with patch.object(media_cache.clients, "download", side_effect=broken):
            with self.assertRaises(requests.ConnectionError):
                self.fetch_photo(self.root / "photo.jpg")
        self.assertFalse((self.root / "photo.jpg").exists())
        self.assertEqual(self.media_files(), [])
        self.assertEqual(list(self.root.rglob(".stock-*.jpg")), [])

    def test_media_cache_ttl_and_byte_cap(self):
        size = self.source.stat().st_size
        with patch.object(config, "STOCK_CACHE_MAX_BYTES", size), \
                patch.object(media_cache.clients, "download", side_effect=self.photo_download) as dl:
            self.fetch_photo(self.root / "one.jpg", "one")
            self.fetch_photo(self.root / "two.jpg", "two")
            self.assertEqual(len(self.media_files()), 1)
            old = time.time() - 604801
            os.utime(self.media_files()[0], (old, old))
            self.fetch_photo(self.root / "again.jpg", "two")
        self.assertEqual(dl.call_count, 3)
        self.assertLessEqual(sum(p.stat().st_size for p in self.media_files()), size)

    def test_oversized_media_is_not_retained(self):
        with patch.object(config, "STOCK_CACHE_MAX_BYTES", 1), \
                patch.object(media_cache.clients, "download", side_effect=self.photo_download):
            result = self.fetch_photo(self.root / "photo.jpg")
        self.assertTrue(media_cache.valid_media(result, "photo"))
        self.assertEqual(self.media_files(), [])

    def test_pruning_removes_only_expired_owned_files(self):
        cache_root = self.root / ".stock-cache" / "media"
        cache_root.mkdir(parents=True)
        stale_temp = cache_root / ".stock-crashed.mp4"
        fresh_temp = cache_root / ".stock-active.mp4"
        other = cache_root / "personal.mp4"
        for path in (stale_temp, fresh_temp, other):
            path.write_bytes(b"untouched")
        old = time.time() - 86401
        os.utime(stale_temp, (old, old))
        media_cache._prune(cache_root)
        self.assertFalse(stale_temp.exists())
        self.assertTrue(fresh_temp.exists())
        self.assertTrue(other.exists())

    def test_disable_media_cache_still_validates_download(self):
        with patch.object(config, "STOCK_CACHE_MAX_BYTES", 0), \
                patch.object(media_cache.clients, "download", side_effect=self.photo_download) as dl:
            self.fetch_photo(self.root / "one.jpg")
            self.fetch_photo(self.root / "two.jpg")
        self.assertEqual(dl.call_count, 2)
        self.assertFalse((self.root / ".stock-cache").exists())

    def test_missing_video_stream_is_rejected_even_with_duration(self):
        source = self.root / "audio-only.mp4"
        source.write_bytes(b"container")
        result = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "streams": [], "format": {"duration": "5.0"}}))
        with patch.object(media_cache.subprocess, "run", return_value=result) as probe:
            self.assertFalse(media_cache.valid_media(source, "video"))
        self.assertIn("v:0", probe.call_args.args[0])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg needed")
    def test_real_video_probe_accepts_finalized_video_and_rejects_truncation(self):
        path = self.root / "tiny.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=black:s=64x64:r=10:d=0.2", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(path)], check=True, timeout=30)
        self.assertTrue(media_cache.valid_media(path, "video"))
        path.write_bytes(path.read_bytes()[:32])
        self.assertFalse(media_cache.valid_media(path, "video"))

    def test_pixabay_selects_smallest_adequate_variant(self):
        variants = {name: {"width": width, "url": "https://cdn.example/" + name}
                    for name, width in (("large", 3840), ("medium", 1920), ("small", 1280))}
        with patch.object(config, "PIXABAY_API_KEY", "test-key"), \
                patch.object(media_cache, "search_json", return_value={
                    "hits": [{"id": 4, "duration": 12, "tags": "car", "videos": variants}]}), \
                patch.object(footage, "_download", return_value=self.root / "car.mp4") as dl:
            footage._pixabay_videos("car", 1920)[0].download(self.root / "car.mp4")
        self.assertEqual(dl.call_args.args[0], "https://cdn.example/medium")

    def test_gather_queries_both_providers_concurrently_but_keeps_order(self):
        gate = threading.Barrier(2)
        def first(query):
            gate.wait(timeout=5)
            return ["first"]
        def second(query):
            gate.wait(timeout=5)
            return ["second"]
        self.assertEqual(footage._gather("car", (first, second)), ["first", "second"])

    def test_cache_does_not_bypass_used_asset_limits(self):
        first, second = Mock(return_value=self.root / "first.mp4"), Mock(return_value=self.root / "second.mp4")
        hits = [footage.Hit("v:1", 1, "car", "video", 10, None, first),
                footage.Hit("pxv:2", 0.8, "car", "video", 10, None, second)]
        used = {}
        for _ in range(2):
            footage._select(hits, self.root / "dest.mp4", 0.4, used, "", "car", "off")
        self.assertEqual(used, {"v:1": 1, "pxv:2": 1})
        self.assertIsNone(footage._select(hits, self.root / "dest.mp4", 0.4, used, "", "car", "off"))
        first.assert_called_once()
        second.assert_called_once()

    def test_job_cancellation_is_not_swallowed_as_download_failure(self):
        fail = Mock(side_effect=cancel.JobCancelled("stopped"))
        hits = [footage.Hit("v:1", 1, "car", "video", 10, None, fail)]
        with self.assertRaises(cancel.JobCancelled):
            footage._select(hits, self.root / "dest.mp4", 0.4, {}, "", "car", "off")

    def test_vision_failure_is_not_downloaded_again_in_keyword_fallback(self):
        broken = Mock(side_effect=ValueError("invalid media"))
        good = Mock(return_value=self.root / "good.mp4")
        hits = [footage.Hit("v:1", 1, "car", "video", 10, None, broken),
                footage.Hit("v:2", 0.9, "car", "video", 10, None, good)]
        with patch("pipeline.curator.choose", side_effect=[0, None]):
            path = footage._select(hits, self.root / "dest.mp4", 0.4, {}, "", "car", "vision")
        self.assertEqual(path, self.root / "good.mp4")
        broken.assert_called_once()
        good.assert_called_once()


if __name__ == "__main__":
    unittest.main()
