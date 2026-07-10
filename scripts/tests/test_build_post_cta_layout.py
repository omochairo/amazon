"""Unit tests for #1980 — A-7 query intent (data/analytics/query_intent.json) →
CTA layout front matter switch in build_post.

_load_query_intent_map() must degrade gracefully (missing/empty/malformed file
=> empty dict) so pages without a classified intent always fall back to the
current (unchanged) layout. _resolve_page_asin() must match the same ASIN
that _frontmatter_meta() puts into ``url`` so the query_intent lookup key
lines up.
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

from build_post import _load_query_intent_map, _resolve_page_asin  # type: ignore[import-not-found]


class LoadQueryIntentMapTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(_load_query_intent_map(pathlib.Path("does/not/exist.json")), {})

    def test_malformed_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "query_intent.json"
            p.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(_load_query_intent_map(p), {})

    def test_commercial_and_informational_are_kept(self):
        payload = {
            "detected": [
                {"page": "https://navi.omcha.jp/products/b0gl815n6k/", "dominant_intent": "informational"},
                {"page": "https://navi.omcha.jp/ranking/", "dominant_intent": "commercial"},
            ]
        }
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "query_intent.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            result = _load_query_intent_map(p)
        self.assertEqual(result, {
            "/products/b0gl815n6k/": "informational",
            "/ranking/": "commercial",
        })

    def test_navigational_is_dropped(self):
        # navigational has no CTA treatment in build_post; must not leak through.
        payload = {"detected": [
            {"page": "https://navi.omcha.jp/products/b000000001/", "dominant_intent": "navigational"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "query_intent.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(_load_query_intent_map(p), {})

    def test_rows_missing_page_or_intent_are_skipped(self):
        payload = {"detected": [
            {"page": "", "dominant_intent": "commercial"},
            {"page": "https://navi.omcha.jp/products/b000000002/"},
            "not-a-dict",
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "query_intent.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(_load_query_intent_map(p), {})

    def test_path_lookup_key_is_lowercased(self):
        payload = {"detected": [
            {"page": "https://navi.omcha.jp/PRODUCTS/B0GL815N6K/", "dominant_intent": "commercial"},
        ]}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "query_intent.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            result = _load_query_intent_map(p)
        self.assertEqual(result, {"/products/b0gl815n6k/": "commercial"})


class ResolvePageAsinTests(unittest.TestCase):
    def test_uses_product_asin_when_present(self):
        self.assertEqual(_resolve_page_asin({"asin": "B0ABCDEFGH"}, "some-slug"), "B0ABCDEFGH")

    def test_falls_back_to_slug_suffix(self):
        self.assertEqual(
            _resolve_page_asin({}, "2026-05-13-B00QER4Y1E"), "B00QER4Y1E"
        )

    def test_falls_back_to_slug_when_no_asin_pattern(self):
        self.assertEqual(_resolve_page_asin(None, "some-slug-without-asin"), "some-slug-without-asin")

    def test_matches_frontmatter_url_key_shape(self):
        # #1980: query_intent lookup key must be f"/products/{asin.lower()}/",
        # matching the url written by _frontmatter_meta.
        asin = _resolve_page_asin({"asin": "B0GL815N6K"}, "2026-06-01-B0GL815N6K")
        self.assertEqual(f"/products/{asin.lower()}/", "/products/b0gl815n6k/")


if __name__ == "__main__":
    unittest.main()
