"""resolve_ranking_asins.py の単体テスト (Issue #810 Phase 1)。

Creator API は呼ばずに FakeAPI でモックする。カバレッジ:
1. _collect_unmatched_jans: matched 済を除外し未マッチ JAN を rank 順・重複排除で抽出
2. resolve_jan_to_asin: externalIds.eans の JAN 一致で ASIN 抽出 / 不一致は ""
3. resolve_ranking_asins: resolved / unresolved / already-covered / 同 ASIN 重複の分岐
4. --limit による解決上限
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import resolve_ranking_asins as rr  # noqa: E402


class FakeAPI:
    """search_items(keywords=JAN) → JAN→ASIN マップを引いて searchResult を返す。"""

    def __init__(self, jan_to_asin: dict, extra_noise: bool = True):
        self.jan_to_asin = jan_to_asin
        self.extra_noise = extra_noise
        self.calls = []

    def search_items(self, keywords=None, search_index="Toys", item_count=10,
                     item_page=1, resources=None):
        self.calls.append(keywords)
        items = []
        # 先頭に JAN 不一致のノイズ item を混ぜ、eans 照合が効くことを検証する。
        if self.extra_noise:
            items.append({
                "asin": "BNOISE0001",
                "itemInfo": {"externalIds": {"eans": {"displayValues": ["4900000000000"]}}},
            })
        asin = self.jan_to_asin.get(keywords)
        if asin:
            items.append({
                "asin": asin,
                "itemInfo": {"externalIds": {"eans": {"displayValues": [keywords]}}},
            })
        return {"searchResult": {"items": items}}


class CollectUnmatchedJansTest(unittest.TestCase):
    def test_skips_matched_and_dedups_in_rank_order(self):
        items = [
            {"rank": 1, "matched_asin": "B0EXIST", "itemCaption": "JAN 4904810000001"},
            {"rank": 2, "matched_asin": None, "title": "ブロック 4904810000002"},
            {"rank": 3, "matched_asin": None, "itemCaption": "コード 4904810000002 重複"},
            {"rank": 4, "matched_asin": None, "title": "JAN なし商品"},
            {"rank": 5, "matched_asin": None, "itemCaption": "EAN 4904810000003 です"},
        ]
        jans = rr._collect_unmatched_jans(items)
        self.assertEqual(
            jans,
            [("4904810000002", 2, "ブロック 4904810000002"),
             ("4904810000003", 5, None)],
        )


class ResolveJanToAsinTest(unittest.TestCase):
    def test_matches_via_externalids(self):
        api = FakeAPI({"4904810000002": "B0NEW002"})
        self.assertEqual(rr.resolve_jan_to_asin(api, "4904810000002"), "B0NEW002")

    def test_no_match_returns_empty(self):
        api = FakeAPI({})
        self.assertEqual(rr.resolve_jan_to_asin(api, "4904810999999"), "")

    def test_api_exception_returns_empty(self):
        class BoomAPI:
            def search_items(self, **kw):
                raise RuntimeError("boom")
        self.assertEqual(rr.resolve_jan_to_asin(BoomAPI(), "4904810000002"), "")


class ResolveRankingAsinsTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"rank": 1, "matched_asin": None, "title": "A 4904810000010"},
            {"rank": 2, "matched_asin": None, "title": "B 4904810000020"},
            {"rank": 3, "matched_asin": None, "title": "C 4904810000030"},
        ]

    def test_resolved_unresolved_and_covered_split(self):
        api = FakeAPI({
            "4904810000010": "B0NEW010",   # 新規
            "4904810000020": "B0COVERED",  # 既 covered → skip
            # 4904810000030 はマップ外 → unresolved
        })
        covered = {"B0COVERED"}
        m = rr.resolve_ranking_asins(self.items, api, covered, sleep=0)
        self.assertEqual(m["new_asins"], ["B0NEW010"])
        self.assertEqual([r["jan"] for r in m["unresolved"]], ["4904810000030"])
        self.assertEqual([s["asin"] for s in m["skipped_already_covered"]], ["B0COVERED"])
        self.assertEqual(m["input_unmatched_jans"], 3)

    def test_covered_match_is_case_insensitive(self):
        api = FakeAPI({"4904810000010": "b0new010"})
        m = rr.resolve_ranking_asins(self.items[:1], api, {"B0NEW010"}, sleep=0)
        self.assertEqual(m["new_asins"], [])
        self.assertEqual(len(m["skipped_already_covered"]), 1)

    def test_same_asin_from_two_jans_dedups(self):
        items = [
            {"rank": 1, "matched_asin": None, "title": "A 4904810000010"},
            {"rank": 2, "matched_asin": None, "title": "B 4904810000020"},
        ]
        api = FakeAPI({"4904810000010": "B0SAME", "4904810000020": "B0SAME"})
        m = rr.resolve_ranking_asins(items, api, set(), sleep=0)
        self.assertEqual(m["new_asins"], ["B0SAME"])

    def test_limit_caps_resolution(self):
        api = FakeAPI({
            "4904810000010": "B0NEW010",
            "4904810000020": "B0NEW020",
            "4904810000030": "B0NEW030",
        })
        m = rr.resolve_ranking_asins(self.items, api, set(), limit=1, sleep=0)
        self.assertEqual(m["input_unmatched_jans"], 1)
        self.assertEqual(m["new_asins"], ["B0NEW010"])
        self.assertEqual(api.calls, ["4904810000010"])


class LoadCoveredAsinsTest(unittest.TestCase):
    def test_articles_suffix_and_per_asin_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            adir = pathlib.Path(td) / "articles"
            adir.mkdir()
            (adir / "2026-05-26-B01MUBACGI.json").write_text("{}", encoding="utf-8")
            (adir / "2026-05-26-4910762175.json").write_text("{}", encoding="utf-8")  # ISBN-10
            (adir / "2026-05-26-B01MUBACGI.quality.json").write_text("{}", encoding="utf-8")  # 除外
            (adir / "notes.json").write_text("{}", encoding="utf-8")  # ASIN サフィックス無し
            proot = pathlib.Path(td) / "per_asin"
            (proot / "B0NEWSNAP1").mkdir(parents=True)
            covered = rr._load_covered_asins(str(adir), str(proot))
            self.assertEqual(covered, {"B01MUBACGI", "4910762175", "B0NEWSNAP1"})

    def test_missing_dirs_return_empty(self):
        self.assertEqual(rr._load_covered_asins("/no/such/a", "/no/such/p"), set())


if __name__ == "__main__":
    unittest.main()
