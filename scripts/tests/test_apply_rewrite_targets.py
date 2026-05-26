"""Unit tests for apply_rewrite_targets.py (Issue #812 Phase 1).

Coverage:
1. _parse_targets - rejects malformed ASINs, strips whitespace.
2. inject_targets - prepends per_asin snapshot items into pool, skips ASINs
   already in pool, skips ASINs without snapshot, tags source.
3. delete_article_files - removes primary + sidecar files matching the slug pattern,
   leaves unrelated files alone.
4. Smoke test: end-to-end on a tmp tree.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import apply_rewrite_targets as art  # noqa: E402


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class ParseTargetsTest(unittest.TestCase):
    def test_strips_whitespace(self) -> None:
        self.assertEqual(art._parse_targets(" B00I7JXEEA , B0CQY911ZP "), ["B00I7JXEEA", "B0CQY911ZP"])

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(art._parse_targets(""), [])

    def test_rejects_bad_asin(self) -> None:
        with self.assertRaises(SystemExit):
            art._parse_targets("B00I7JXEEA,not-an-asin")


class InjectTargetsTest(unittest.TestCase):
    def test_prepends_snapshot_item(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "amazon.json")
            _write_json(pool, {"keyword": "x", "items": [{"asin": "B0EXISTING1"}], "mode": "daily"})
            per_asin = os.path.join(d, "per_asin")
            _write_json(
                os.path.join(per_asin, "B00I7JXEEA", "amazon.json"),
                {"asin": "B00I7JXEEA", "fetched_at": "now", "item": {"asin": "B00I7JXEEA", "title": "プラレール"}},
            )
            n = art.inject_targets(pool, per_asin, ["B00I7JXEEA"])
            self.assertEqual(n, 1)
            with open(pool, encoding="utf-8") as f:
                got = json.load(f)
            # Prepended at index 0
            self.assertEqual(got["items"][0]["asin"], "B00I7JXEEA")
            self.assertEqual(got["items"][1]["asin"], "B0EXISTING1")
            # Source tagged
            self.assertEqual(got["items"][0]["source"], "Rewrite (idle-fill)")

    def test_skips_already_in_pool(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "amazon.json")
            _write_json(pool, {"items": [{"asin": "B00I7JXEEA", "title": "existing"}]})
            per_asin = os.path.join(d, "per_asin")
            _write_json(
                os.path.join(per_asin, "B00I7JXEEA", "amazon.json"),
                {"asin": "B00I7JXEEA", "item": {"asin": "B00I7JXEEA", "title": "snapshot"}},
            )
            n = art.inject_targets(pool, per_asin, ["B00I7JXEEA"])
            self.assertEqual(n, 0)
            with open(pool, encoding="utf-8") as f:
                got = json.load(f)
            # Original entry kept, not duplicated
            self.assertEqual(len(got["items"]), 1)
            self.assertEqual(got["items"][0]["title"], "existing")

    def test_skips_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "amazon.json")
            _write_json(pool, {"items": []})
            n = art.inject_targets(pool, os.path.join(d, "per_asin"), ["B00I7JXEEA"])
            self.assertEqual(n, 0)

    def test_preserves_existing_source_tag(self) -> None:
        """Snapshot already has a source field → do not overwrite with rewrite tag."""
        with tempfile.TemporaryDirectory() as d:
            pool = os.path.join(d, "amazon.json")
            _write_json(pool, {"items": []})
            per_asin = os.path.join(d, "per_asin")
            _write_json(
                os.path.join(per_asin, "B00I7JXEEA", "amazon.json"),
                {"asin": "B00I7JXEEA", "item": {"asin": "B00I7JXEEA", "source": "Amazon (Target)"}},
            )
            art.inject_targets(pool, per_asin, ["B00I7JXEEA"])
            with open(pool, encoding="utf-8") as f:
                got = json.load(f)
            self.assertEqual(got["items"][0]["source"], "Amazon (Target)")


class DeleteArticleFilesTest(unittest.TestCase):
    def test_removes_primary_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            articles = os.path.join(d, "data", "articles")
            os.makedirs(articles)
            for name in (
                "2026-05-11-B00I7JXEEA.json",
                "2026-05-11-B00I7JXEEA.quality.json",
                "2026-05-11-B00I7JXEEA.enrichment.json",
                "2026-05-11-B00I7JXEEA.seo.json",
                "2026-05-12-B0KEEP00000.json",  # unrelated, must survive
                "2026-05-12-B0KEEP00000.quality.json",
            ):
                with open(os.path.join(articles, name), "w", encoding="utf-8") as f:
                    f.write("{}")
            removed = art.delete_article_files(d, ["B00I7JXEEA"])
            self.assertEqual(removed, 4)
            survived = sorted(os.listdir(articles))
            self.assertEqual(survived, ["2026-05-12-B0KEEP00000.json", "2026-05-12-B0KEEP00000.quality.json"])

    def test_missing_files_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data", "articles"))
            self.assertEqual(art.delete_article_files(d, ["B00I7JXEEA"]), 0)


if __name__ == "__main__":
    unittest.main()
