"""Unit tests for #2812 — enforce verified amazon.json image over Jules hallucination.

Covers _enforce_amazon_image:
    - overwrites a hallucinated (404) product.image with the verified amazon.json image
    - rebuilds product.images gallery from the verified array (primary first)
    - returns False / leaves Jules value intact when amazon.json has no image
    - reads from the per-ASIN snapshot when the raw index lacks the ASIN
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _enforce_amazon_image  # type: ignore[import-not-found]

VERIFIED = "https://m.media-amazon.com/images/I/41NZsBwXgVL._SL500_.jpg"
VERIFIED2 = "https://m.media-amazon.com/images/I/41yLMvnsJSL._SL500_.jpg"
HALLUCINATED = "https://m.media-amazon.com/images/I/71+G9T+GzBL._AC_SX679_.jpg"


class EnforceAmazonImageTests(unittest.TestCase):
    def test_overwrites_hallucinated_image_from_raw_index(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
        raw_index = {"B0G5DLSTS8": {"image": VERIFIED, "images": [VERIFIED, VERIFIED2]}}
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertTrue(changed)
        self.assertEqual(data["product"]["image"], VERIFIED)
        # gallery rebuilt from verified array, primary first
        self.assertEqual(data["product"]["images"][0], VERIFIED)
        self.assertNotIn(HALLUCINATED, data["product"]["images"])

    def test_no_change_when_already_correct(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": VERIFIED}}
        raw_index = {"B0G5DLSTS8": {"image": VERIFIED}}
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertFalse(changed)
        self.assertEqual(data["product"]["image"], VERIFIED)

    def test_keeps_jules_value_when_amazon_has_no_image(self):
        data = {"product": {"asin": "B0NOIMG", "image": HALLUCINATED}}
        raw_index = {"B0NOIMG": {"price": 1000}}
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertFalse(changed)
        self.assertEqual(data["product"]["image"], HALLUCINATED)

    def test_falls_back_to_images_zero_when_image_field_missing(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
        raw_index = {"B0G5DLSTS8": {"images": [VERIFIED, VERIFIED2]}}
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertTrue(changed)
        self.assertEqual(data["product"]["image"], VERIFIED)

    def test_reads_per_asin_snapshot_when_raw_index_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "B0G5DLSTS8").mkdir()
            (root / "B0G5DLSTS8" / "amazon.json").write_text(
                json.dumps({"item": {"image": VERIFIED, "images": [VERIFIED]}}),
                encoding="utf-8",
            )
            data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
            changed = _enforce_amazon_image(data, {}, root)
            self.assertTrue(changed)
            self.assertEqual(data["product"]["image"], VERIFIED)

    def test_no_product_or_asin_is_safe(self):
        self.assertFalse(_enforce_amazon_image({}, {}, pathlib.Path("/x")))
        self.assertFalse(
            _enforce_amazon_image({"product": {"image": HALLUCINATED}}, {}, pathlib.Path("/x"))
        )

    # #3314: hero image width/height passthrough
    def test_attaches_image_dimensions_from_raw_index(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
        raw_index = {
            "B0G5DLSTS8": {
                "image": VERIFIED,
                "images": [VERIFIED],
                "image_width": 500,
                "image_height": 375,
            }
        }
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertTrue(changed)
        self.assertEqual(data["product"]["image_width"], 500)
        self.assertEqual(data["product"]["image_height"], 375)

    def test_falls_back_to_per_asin_snapshot_dimensions(self):
        # raw_index has the image URL but no dims (e.g. freshly fetched this
        # run, before write_per_asin_snapshot's cache backfill); per-ASIN
        # snapshot has the same image URL plus dims.
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "B0G5DLSTS8").mkdir()
            (root / "B0G5DLSTS8" / "amazon.json").write_text(
                json.dumps({"item": {
                    "image": VERIFIED,
                    "image_width": 500,
                    "image_height": 500,
                }}),
                encoding="utf-8",
            )
            data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
            raw_index = {"B0G5DLSTS8": {"image": VERIFIED}}
            changed = _enforce_amazon_image(data, raw_index, root)
            self.assertTrue(changed)
            self.assertEqual(data["product"]["image_width"], 500)
            self.assertEqual(data["product"]["image_height"], 500)

    def test_no_dimensions_leaves_attribute_unset(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED, "image_width": 999}}
        raw_index = {"B0G5DLSTS8": {"image": VERIFIED}}
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertTrue(changed)
        self.assertNotIn("image_width", data["product"])
        self.assertNotIn("image_height", data["product"])

    def test_ignores_malformed_dimensions(self):
        data = {"product": {"asin": "B0G5DLSTS8", "image": HALLUCINATED}}
        raw_index = {
            "B0G5DLSTS8": {"image": VERIFIED, "image_width": "500", "image_height": None}
        }
        changed = _enforce_amazon_image(data, raw_index, pathlib.Path("/nonexistent"))
        self.assertTrue(changed)
        self.assertNotIn("image_width", data["product"])
        self.assertNotIn("image_height", data["product"])


if __name__ == "__main__":
    unittest.main()
