"""Unit tests for fetch_amazon._attach_image_dimensions (#3314).

Covers: cache reuse when the per-ASIN snapshot already has dims for the same
image URL (no network call), a fresh fetch on cache miss, fail-soft when the
fetch fails, and skipping entirely when there is no image URL.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_amazon as fa  # noqa: E402

IMG = "https://m.media-amazon.com/images/I/51lgdFCq-UL._SL500_.jpg"
IMG2 = "https://m.media-amazon.com/images/I/other._SL500_.jpg"


class AttachImageDimensionsTests(unittest.TestCase):
    def test_no_image_url_is_noop(self):
        item = {"asin": "B0X", "image": ""}
        with mock.patch("image_dimensions.fetch_image_dimensions") as m:
            fa._attach_image_dimensions("/nonexistent", item)
        m.assert_not_called()
        self.assertNotIn("image_width", item)

    def test_cache_hit_skips_network(self):
        with tempfile.TemporaryDirectory() as td:
            asin_dir = os.path.join(td, "per_asin", "B0X")
            os.makedirs(asin_dir)
            with open(os.path.join(asin_dir, "amazon.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {"item": {"image": IMG, "image_width": 500, "image_height": 400}}, f
                )
            item = {"asin": "B0X", "image": IMG}
            with mock.patch("image_dimensions.fetch_image_dimensions") as m:
                fa._attach_image_dimensions(td, item)
            m.assert_not_called()
            self.assertEqual(item["image_width"], 500)
            self.assertEqual(item["image_height"], 400)

    def test_cache_miss_on_url_change_triggers_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            asin_dir = os.path.join(td, "per_asin", "B0X")
            os.makedirs(asin_dir)
            with open(os.path.join(asin_dir, "amazon.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {"item": {"image": IMG, "image_width": 500, "image_height": 400}}, f
                )
            item = {"asin": "B0X", "image": IMG2}
            with mock.patch(
                "image_dimensions.fetch_image_dimensions", return_value=(300, 200)
            ) as m, mock.patch("time.sleep"):
                fa._attach_image_dimensions(td, item)
            m.assert_called_once_with(IMG2)
            self.assertEqual(item["image_width"], 300)
            self.assertEqual(item["image_height"], 200)

    def test_fetch_failure_is_fail_soft(self):
        item = {"asin": "B0NEW", "image": IMG}
        with mock.patch(
            "image_dimensions.fetch_image_dimensions", return_value=None
        ), mock.patch("time.sleep"):
            fa._attach_image_dimensions("/nonexistent", item)
        self.assertNotIn("image_width", item)
        self.assertNotIn("image_height", item)


if __name__ == "__main__":
    unittest.main()
