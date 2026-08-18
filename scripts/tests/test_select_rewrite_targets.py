"""Unit tests for select_rewrite_targets.py (Issue #812 Phase 1).

Coverage:
1. collect_candidates - parses primary article JSONs, skips .quality/.enrichment/.seo
   sidecars and slugs that don't match YYYY-MM-DD-ASIN。
   #4826 項目4: quality sidecar の読み取り (_load_quality) は削除した。sidecar の
   生成は quality_gate 側で廃止済みで実体も 0 件、読んでも必ず (None, None) が
   返るだけの経路だった。順序への影響は 2026-08-09 に既に外してある。
2. select - priority key: pre-v7 (slug date < HOW_TO_CHOOSE_ENFORCE_FROM) ranks
   above post-v7; within the same generation, older date wins; total_score は
   順序に影響しない (2026-08-09 に外した。理由は select_rewrite_targets の docstring);
   exclude-set filters ASINs.
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

            # #4826 項目4: 候補レコードに score / passed は載らなくなった。
            by_asin = {c["asin"]: c for c in got}
            self.assertEqual(sorted(by_asin["B00I7JXEEA"]), ["asin", "date", "slug"])

    def test_duplicate_asin_kept_once(self) -> None:
        """Two article files referencing the same ASIN: only the earlier one is kept."""
        with tempfile.TemporaryDirectory() as d:
            _write_article(d, "2026-05-11-B00I7JXEEA", {"total_score": 80, "passed": False})
            _write_article(d, "2026-05-20-B00I7JXEEA", {"total_score": 95, "passed": True})
            got = srt.collect_candidates(d)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["slug"], "2026-05-11-B00I7JXEEA")


class SelectTest(unittest.TestCase):
    def test_score_does_not_affect_order(self) -> None:
        """total_score は順序に影響しない (2026-08-09 に順序キーから外した)。

        旧実装は「sidecar 欠落 = score None → -1 で最優先」だったが、sidecar は
        2026-05-28 を最後に生成が止まっており、実際に並べていたのは品質ではなく
        sidecar 生成が止まった日だった。sidecar を持つ最古の 233 件が、より新しい
        1,669 件の後ろへ回されていた。
        """
        candidates = [
            {"slug": "2026-05-12-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-12", "score": 98, "passed": True},
            {"slug": "2026-05-20-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-05-20", "score": None, "passed": None},
            {"slug": "2026-05-11-B0CCCCCCCC", "asin": "B0CCCCCCCC", "date": "2026-05-11", "score": 50, "passed": False},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0CCCCCCCC", "B0AAAAAAAA", "B0BBBBBBBB"])

    def test_pre_v7_outranks_post_v7_even_when_newer_has_no_sidecar(self) -> None:
        """v7 施行日より前の記事が先。新しい記事は sidecar が無くても後回し。"""
        candidates = [
            {"slug": "2026-08-01-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-08-01", "score": None, "passed": None},
            {"slug": "2026-07-16-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-07-16", "score": None, "passed": None},
            {"slug": "2026-05-14-B0CCCCCCCC", "asin": "B0CCCCCCCC", "date": "2026-05-14", "score": 97, "passed": True},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        # 2026-07-16 は施行日ちょうど = post_v7 (quality_gate._how_to_choose_enforced と同じ境界)
        self.assertEqual([c["asin"] for c in picked], ["B0CCCCCCCC", "B0BBBBBBBB", "B0AAAAAAAA"])

    def test_same_generation_tie_broken_by_date(self) -> None:
        candidates = [
            {"slug": "2026-05-20-B0AAAAAAAA", "asin": "B0AAAAAAAA", "date": "2026-05-20", "score": 80, "passed": False},
            {"slug": "2026-05-11-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-05-11", "score": 80, "passed": False},
            {"slug": "2026-05-15-B0CCCCCCCC", "asin": "B0CCCCCCCC", "date": "2026-05-15", "score": 80, "passed": False},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0BBBBBBBB", "B0CCCCCCCC", "B0AAAAAAAA"])

    def test_undated_candidate_is_treated_as_post_v7(self) -> None:
        """日付不明は安全側 (post_v7 = 後回し)。優先枠を不明な候補に食わせない。"""
        candidates = [
            {"slug": "bogus", "asin": "B0AAAAAAAA", "date": "", "score": None, "passed": None},
            {"slug": "2026-06-01-B0BBBBBBBB", "asin": "B0BBBBBBBB", "date": "2026-06-01", "score": None, "passed": None},
        ]
        picked = srt.select(candidates, excluded=set(), limit=10)
        self.assertEqual([c["asin"] for c in picked], ["B0BBBBBBBB", "B0AAAAAAAA"])

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
