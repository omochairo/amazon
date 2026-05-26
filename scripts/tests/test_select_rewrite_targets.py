"""Unit tests for select_rewrite_targets.py (Issue #812 Phase 1).

Coverage:
1. collect_candidates - parses primary article JSONs, skips .quality/.enrichment/.seo
   sidecars and slugs that don't match YYYY-MM-DD-ASIN.
2. _load_quality - reads total_score + passed; missing/bad files → (None, None).
3. select - priority key: missing quality.json (None) ranks ABOVE numeric scores;
   within same score, older date wins; exclude-set filters ASINs.
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

import select_rewrite_targets as srt  # noqa: E402


def _write_article(d: str, slug: str, quality: dict | None = None) -> None:
    """Create a minimal primary article JSON + optional quality sidecar."""
    with open(os.path.join(d, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "title": "x"}, f)
    if quality is not None:
        with open(os.path.join(d, f"{slug}.quality.json"), "w", encoding="utf-8") as f:
            json.dump(quality, f)


class CollectCandidatesTest(unittest.TestCase):
    def test_skips_sidecars_and_bad_slugs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _write_article(d, "2026-05-11-B00I7JXEEA", {"total_score": 92, "passed": False})
            _write_article(d, "2026-05-12-B0CQY911ZP", {"total_score": 98, "passed": True})
            _write_article(d, "2026-05-13-B073W9V2WB", None)
            # Sidecars / non-conforming slugs that must be filtered out:
            with open(os.path.join(d, "not-a-slug.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(d, "2026-05-12-B0CQY911ZP.enrichment.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(d, "2026-05-12-B0CQY911ZP.seo.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            got = srt.collect_candidates(d)
            asins = sorted(c["asin"] for c in got)
            self.assertEqual(asins, ["B00I7JXEEA", "B073W9V2WB", "B0CQY911ZP"])

            by_asin = {c["asin"]: c for c in got}
            self.assertEqual(by_asin["B00I7JXEEA"]["score"], 92)
            self.assertFalse(by_asin["B00I7JXEEA"]["passed"])
            self.assertEqual(by_asin["B0CQY911ZP"]["score"], 98)
            self.assertIsNone(by_asin["B073W9V2WB"]["score"])
            self.assertIsNone(by_asin["B073W9V2WB"]["passed"])

    def test_duplicate_asin_kept_once(self) -> None:
        """Two article files referencing the same ASIN: only the earlier one is kept."""
        with tempfile.TemporaryDirectory() as d:
            _write_article(d, "2026-05-11-B00I7JXEEA", {"total_score": 80, "passed": False})
            _write_article(d, "2026-05-20-B00I7JXEEA", {"total_score": 95, "passed": True})
            got = srt.collect_candidates(d)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["slug"], "2026-05-11-B00I7JXEEA")


class LoadQualityTest(unittest.TestCase):
    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(srt._load_quality("ghost", d), (None, None))

    def test_bad_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.quality.json"), "w", encoding="utf-8") as f:
                f.write("not json {")
            self.assertEqual(srt._load_quality("x", d), (None, None))

    def test_non_numeric_score_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.quality.json"), "w", encoding="utf-8") as f:
                json.dump({"total_score": "high", "passed": "yes"}, f)
            self.assertEqual(srt._load_quality("x", d), (None, None))


class SelectTest(unittest.TestCase):
    def test_missing_score_outranks_numeric(self) -> None:
        """A missing quality.json (score=None) should rank above any numeric score."""
        candidates = [
            {"slug": "2026-05-12-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-12", "score": 50, "passed": False},
            {"slug": "2026-05-20-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-05-20", "score": None, "passed": None},
            {"slug": "2026-05-11-B0CCCCCCCC", "asin": "B0CCCCCCCC", "date": "2026-05-11", "score": 98, "passed": True},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0BBBBBBBB", "B0AAAAAAAA", "B0CCCCCCCC"])

    def test_score_tie_broken_by_date(self) -> None:
        candidates = [
            {"slug": "2026-05-20-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-20", "score": 80, "passed": False},
            {"slug": "2026-05-11-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-05-11", "score": 80, "passed": False},
            {"slug": "2026-05-15-B0CCCCCCCC", "asin": "B0CCCCCCCC", "date": "2026-05-15", "score": 80, "passed": False},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0BBBBBBBB", "B0CCCCCCCC", "B0AAAAAAAA"])

    def test_exclude_filters_asin(self) -> None:
        candidates = [
            {"slug": "2026-05-12-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-12", "score": 50, "passed": False},
            {"slug": "2026-05-13-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-05-13", "score": 60, "passed": False},
        ]
        picked = srt.select(candidates, excluded={"B0AAAAAAAA"}, limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0BBBBBBBB"])

    def test_limit_caps_output(self) -> None:
        candidates = [
            {"slug": f"2026-05-{i:02d}-B0{i:08d}", "asin": f"B0{i:08d}", "date": f"2026-05-{i:02d}", "score": 50, "passed": False}
            for i in range(1, 11)
        ]
        picked = srt.select(candidates, excluded=set(), limit=3)
        self.assertEqual(len(picked), 3)

    def test_limit_zero_returns_empty(self) -> None:
        candidates = [{"slug": "2026-05-12-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-12", "score": 50, "passed": False}]
        self.assertEqual(srt.select(candidates, excluded=set(), limit=0), [])


class ReadExcludeTest(unittest.TestCase):
    def test_extracts_asins_from_freeform_text(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ex.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("jules-lock/B0AAAAAAAA\n")
                f.write("[feat] generate B0BBBBBBBB article\n")
                f.write("no asin here\n")
                f.write("B0CCCCCCCC and B0DDDDDDDD same line\n")
            got = srt._read_exclude(p)
            self.assertEqual(got, {"B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC", "B0DDDDDDDD"})

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(srt._read_exclude("/nonexistent/path"), set())
        self.assertEqual(srt._read_exclude(""), set())


if __name__ == "__main__":
    unittest.main()
