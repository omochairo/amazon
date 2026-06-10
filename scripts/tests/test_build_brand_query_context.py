"""test_build_brand_query_context.py — Phase 3A (#1329)

build_brand_query_context.py の純関数を mock スナップショットで検証。
GSC 蓄積データの実体に依存せず、マッチング / 閾値 / 正規化 / global hints / fallback を網羅。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from build_brand_query_context import (  # noqa: E402
    build_context,
    global_seo_hints,
    normalize,
    normalize_page_path,
    _brand_page_match,
    _query_matches_brand,
)


def _snap(by_query=None, by_combo=None):
    return {"by_query": by_query or [], "by_combo": by_combo or []}


def _q(query, impressions, clicks=0):
    return {"query": query, "impressions": impressions, "clicks": clicks}


def _c(query, page, impressions, clicks=0):
    return {"query": query, "page": page, "impressions": impressions, "clicks": clicks}


class TestNormalize(unittest.TestCase):
    def test_nfkc_lower_and_bracket_strip(self):
        # 全角→半角 + lower + 括弧除去
        self.assertEqual(normalize("ベイブレードＸ"), "ベイブレードx")
        self.assertEqual(normalize("わくわく ()"), "わくわく")
        self.assertEqual(normalize("Fisher-Price"), "fisher-price")

    def test_page_path_decode_and_lower(self):
        # URL encode された日本語 brand segment を decode
        p = normalize_page_path(
            "https://navi.omcha.jp/brands/%E3%83%AD%E3%83%B3%E3%83%93%E3%83%BC/")
        self.assertEqual(p, "/brands/ロンビー/")
        # ASIN は小文字化 (双方向マッチ用)
        self.assertEqual(
            normalize_page_path("https://navi.omcha.jp/products/B06Y14CCW3/"),
            "/products/b06y14ccw3/")


class TestMatchHelpers(unittest.TestCase):
    def test_query_substring(self):
        terms = {"ロンビー"}
        self.assertTrue(_query_matches_brand(normalize("ロンビー 知育"), terms))
        self.assertFalse(_query_matches_brand(normalize("レゴ ブロック"), terms))

    def test_brand_page_match_series(self):
        terms = {"タカラトミー"}
        # シリーズ配下も brand segment 一致で True
        self.assertTrue(_brand_page_match("/brands/タカラトミー/ベイブレード/", terms))
        self.assertFalse(_brand_page_match("/products/b000/", terms))
        # /brands 配下でも別 brand は False
        self.assertFalse(_brand_page_match("/brands/レゴ/", terms))


class TestBuildContext(unittest.TestCase):
    def test_brand_name_match_accumulates_weeks(self):
        snaps = [
            ("2026-W20", _snap(by_query=[_q("ロンビー 知育", 2, 1)])),
            ("2026-W21", _snap(by_query=[_q("ロンビー 知育", 3, 0)])),
        ]
        r = build_context(snaps, "ロンビー", {"ロンビー"})
        self.assertFalse(r["fallback"])
        self.assertEqual(len(r["queries"]), 1)
        q = r["queries"][0]
        self.assertEqual(q["impressions_sum"], 5)
        self.assertEqual(q["clicks_sum"], 1)
        self.assertEqual(q["weeks_seen"], 2)
        self.assertEqual(q["match"], ["brand_name"])

    def test_page_path_match_adds_query_and_page(self):
        snaps = [(
            "2026-W23",
            _snap(
                by_query=[_q("おみせやさん 8歳", 4)],
                by_combo=[_c("おみせやさん 8歳",
                             "https://navi.omcha.jp/brands/%E3%82%A2%E3%82%AC%E3%83%84%E3%83%9E/",
                             4)],
            ),
        )]
        # アガツマ brand page にランディングしたクエリを採用
        r = build_context(snaps, "アガツマ", {"アガツマ"})
        self.assertFalse(r["fallback"])
        self.assertEqual(r["queries"][0]["query"], "おみせやさん 8歳")
        self.assertIn("page_path", r["queries"][0]["match"])
        self.assertEqual(r["page_paths"], ["/brands/アガツマ/"])

    def test_both_sources_no_double_count(self):
        # 同一クエリが brand-name と page_path 両方でヒット → impressions 二重計上しない
        snaps = [(
            "2026-W23",
            _snap(
                by_query=[_q("アガツマ おみせやさん", 6)],
                by_combo=[_c("アガツマ おみせやさん",
                             "https://navi.omcha.jp/brands/%E3%82%A2%E3%82%AC%E3%83%84%E3%83%9E/",
                             6)],
            ),
        )]
        r = build_context(snaps, "アガツマ", {"アガツマ"})
        q = r["queries"][0]
        self.assertEqual(q["impressions_sum"], 6)
        self.assertEqual(sorted(q["match"]), ["brand_name", "page_path"])

    def test_threshold_drops_single_week_low_impressions(self):
        # 単週 impressions=1 / weeks_seen=1 → 閾値未満で drop
        snaps = [("2026-W23", _snap(by_query=[_q("ロンビー", 1)]))]
        r = build_context(snaps, "ロンビー", {"ロンビー"})
        self.assertTrue(r["fallback"])
        self.assertEqual(r["queries"], [])

    def test_threshold_keeps_two_weeks_low_impressions(self):
        # weeks_seen>=2 かつ impressions_sum>=1 → 採用
        snaps = [
            ("2026-W22", _snap(by_query=[_q("ロンビー", 1)])),
            ("2026-W23", _snap(by_query=[_q("ロンビー", 1)])),
        ]
        r = build_context(snaps, "ロンビー", {"ロンビー"})
        self.assertFalse(r["fallback"])
        self.assertEqual(r["queries"][0]["impressions_sum"], 2)
        self.assertEqual(r["queries"][0]["weeks_seen"], 2)

    def test_fullwidth_brand_alias_match(self):
        # クエリ側が全角 X、term が半角 x でも NFKC 正規化で一致
        snaps = [("2026-W23", _snap(by_query=[_q("ベイブレードＸ 評価", 5)]))]
        r = build_context(snaps, "タカラトミー", {"ベイブレードx"})
        self.assertFalse(r["fallback"])
        self.assertEqual(r["queries"][0]["query"], "ベイブレードＸ 評価")

    def test_max_queries_cap_and_sort(self):
        rows = [_q(f"ロンビー {i}", i) for i in range(1, 13)]  # imp 1..12
        snaps = [("2026-W23", _snap(by_query=rows))]
        r = build_context(snaps, "ロンビー", {"ロンビー"}, max_queries=3)
        self.assertEqual(len(r["queries"]), 3)
        # impressions 降順
        self.assertEqual([q["impressions_sum"] for q in r["queries"]], [12, 11, 10])

    def test_no_match_is_fallback(self):
        snaps = [("2026-W23", _snap(by_query=[_q("レゴ ブロック", 9)]))]
        r = build_context(snaps, "ロンビー", {"ロンビー"})
        self.assertTrue(r["fallback"])
        self.assertEqual(r["queries"], [])


class TestGlobalSeoHints(unittest.TestCase):
    def test_extracts_age_and_intent(self):
        snaps = [("2026-W23", _snap(by_query=[
            _q("おままごと おもちゃ 中学年", 9),
            _q("おみせやさん 8歳", 4),
            _q("プログラミング おもちゃ ランキング", 7),
            _q("チェッカー 中学年", 5),
        ]))]
        h = global_seo_hints(snaps)
        self.assertIn("中学年", h["age_keywords_seen"])
        self.assertIn("8歳", h["age_keywords_seen"])
        self.assertIn("ランキング", h["intent_keywords_seen"])
        self.assertTrue(h["rationale"])

    def test_empty_when_no_keywords(self):
        snaps = [("2026-W23", _snap(by_query=[_q("レゴ", 3)]))]
        h = global_seo_hints(snaps)
        self.assertEqual(h["age_keywords_seen"], [])
        self.assertEqual(h["intent_keywords_seen"], [])
        self.assertEqual(h["rationale"], "")


if __name__ == "__main__":
    unittest.main()
