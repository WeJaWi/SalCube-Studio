import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from pipeline import cancel, clients


class Response:
    def __init__(self, blocks, headers=None):
        self.blocks = blocks
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for block in self.blocks:
            if isinstance(block, Exception):
                raise block
            yield block

    def close(self):
        self.closed = True


class DownloadSafetyTests(unittest.TestCase):
    def tearDown(self):
        cancel.set_current(None)

    def test_success_replaces_destination_and_closes_response(self):
        response = Response([b"new", b"-data"], {"Content-Length": "8"})
        with TemporaryDirectory() as tmp, patch.object(clients.requests, "get", return_value=response):
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"old")
            self.assertEqual(clients.download("https://test.invalid", path), path)
            self.assertEqual(path.read_bytes(), b"new-data")
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertTrue(response.closed)

    def test_interrupted_retry_never_publishes_a_partial(self):
        first = Response([b"partial", requests.ConnectionError("lost")])
        second = Response([b"complete"])
        with TemporaryDirectory() as tmp, patch.object(clients.cancel, "sleep"):
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"old")
            def get(*args, **kwargs):
                self.assertEqual(path.read_bytes(), b"old")
                return first if not first.closed else second
            with patch.object(clients.requests, "get", side_effect=get):
                clients.download("https://test.invalid", path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertTrue(first.closed and second.closed)

    def test_truncated_response_preserves_original(self):
        response = Response([b"short"], {"Content-Length": "100"})
        with TemporaryDirectory() as tmp, patch.object(clients.requests, "get", return_value=response), \
                patch.object(clients, "_retrying", side_effect=lambda fn: fn()):
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"old")
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                clients.download("https://test.invalid", path)
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertTrue(response.closed)

    def test_compressed_content_length_is_not_decoded_length(self):
        response = Response([b"decoded"], {"Content-Encoding": "gzip", "Content-Length": "99"})
        with TemporaryDirectory() as tmp, patch.object(clients.requests, "get", return_value=response):
            path = clients.download("https://test.invalid", Path(tmp) / "nested/clip.mp4")
            self.assertEqual(path.read_bytes(), b"decoded")

    def test_cancel_during_download_preserves_destination(self):
        token = cancel.CancelToken("test")
        cancel.set_current(token)
        response = Response([b"new"])
        def blocks(chunk_size):
            token.cancel()
            yield b"partial"
        response.iter_content = blocks
        with TemporaryDirectory() as tmp, patch.object(clients.requests, "get", return_value=response) as get:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"old")
            with self.assertRaises(cancel.JobCancelled):
                clients.download("https://test.invalid", path)
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(path.parent.iterdir()), [path])
            get.assert_called_once()
        self.assertTrue(response.closed)

    def test_empty_download_is_not_published(self):
        with TemporaryDirectory() as tmp, patch.object(clients.requests, "get", return_value=Response([])), \
                patch.object(clients, "_retrying", side_effect=lambda fn: fn()):
            path = Path(tmp) / "clip.mp4"
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                clients.download("https://test.invalid", path)
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.iterdir()), [])
