"""Unit tests for image_dimensions.fetch_image_dimensions (#3314).

Covers the streaming-header-read path (avoid full download), fail-soft
behaviour on HTTP errors / timeouts / garbage bytes, and the max_bytes
give-up cutoff.
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import image_dimensions  # noqa: E402

from PIL import Image  # noqa: E402


def _jpeg_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=(200, 100, 50)).save(buf, format="JPEG")
    return buf.getvalue()


def _chunks(data: bytes, size: int = 4096):
    for i in range(0, len(data), size):
        yield data[i : i + size]


class _FakeResponse:
    def __init__(self, data: bytes, status: int = 200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=4096):
        return _chunks(self._data, chunk_size)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchImageDimensionsTests(unittest.TestCase):
    def test_reads_dimensions_without_full_download(self):
        data = _jpeg_bytes(300, 200)
        with mock.patch("requests.get", return_value=_FakeResponse(data)):
            dims = image_dimensions.fetch_image_dimensions("https://example.com/x.jpg")
        self.assertEqual(dims, (300, 200))

    def test_empty_url_returns_none(self):
        self.assertIsNone(image_dimensions.fetch_image_dimensions(""))

    def test_http_error_is_fail_soft(self):
        with mock.patch("requests.get", return_value=_FakeResponse(b"", status=404)):
            dims = image_dimensions.fetch_image_dimensions("https://example.com/gone.jpg")
        self.assertIsNone(dims)

    def test_network_exception_is_fail_soft(self):
        with mock.patch("requests.get", side_effect=OSError("timeout")):
            dims = image_dimensions.fetch_image_dimensions("https://example.com/x.jpg")
        self.assertIsNone(dims)

    def test_garbage_bytes_give_up_at_max_bytes(self):
        with mock.patch("requests.get", return_value=_FakeResponse(b"\x00" * 2048)):
            dims = image_dimensions.fetch_image_dimensions(
                "https://example.com/x.jpg", max_bytes=1024
            )
        self.assertIsNone(dims)


if __name__ == "__main__":
    unittest.main()
