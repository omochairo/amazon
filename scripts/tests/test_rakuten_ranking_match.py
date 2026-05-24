"""fetch_rakuten.py のランキング・マッチングロジック単体テスト (Issue #600 PR1)。

カバレッジ:
1. _build_itemcode_to_asin: rakuten_matched.json から itemCode -> ASIN 構築
2. _build_jan_to_asin: per_asin/*/amazon.json から jan_code -> ASIN 構築
3. _extract_jan_from_text: 13桁/8桁の JAN を抽出、年号・電話番号は誤検出しない
4. _match_ranking_item: Stage 1 直引き / Stage 2 JAN / 未マッチの分岐
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_rakuten  # noqa: E402


class ItemCodeIndexTest(unittest.TestCase):
    def test_builds_index_from_matched_items(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rakuten_matched.json"
            p.write_text(json.dumps({
                "items": [
                    {"itemCode": "shopa:123", "matched_asin": "B0001"},
                    {"itemCode": "shopb:456", "matched_asin": "B0002"},
                    {"itemCode": "shopc:789", "matched_asin": ""},  # 無効: skip
                    {"itemCode": "", "matched_asin": "B0003"},      # 無効: skip
                ]
            }), encoding="utf-8")
            idx = fetch_rakuten._build_itemcode_to_asin(p)
            self.assertEqual(idx, {"shopa:123": "B0001", "shopb:456": "B0002"})

    def test_returns_empty_when_missing(self):
        self.assertEqual(
            fetch_rakuten._build_itemcode_to_asin(pathlib.Path("/no/such/file")),
            {},
        )

    def test_handles_broken_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rakuten_matched.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(fetch_rakuten._build_itemcode_to_asin(p), {})


class JanIndexTest(unittest.TestCase):
    def test_builds_jan_index_from_per_asin_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for asin, jan in [("B0001", "4904810642602"), ("B0002", "4904810741398"), ("B0003", "")]:
                d = root / asin
                d.mkdir()
                (d / "amazon.json").write_text(
                    json.dumps({"item": {"jan_code": jan}}), encoding="utf-8"
                )
            idx = fetch_rakuten._build_jan_to_asin(root)
            self.assertEqual(idx, {"4904810642602": "B0001", "4904810741398": "B0002"})

    def test_first_wins_on_duplicate_jan(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for asin in ["B0001", "B0002"]:
                d = root / asin
                d.mkdir()
                (d / "amazon.json").write_text(
                    json.dumps({"item": {"jan_code": "4904810000000"}}), encoding="utf-8"
                )
            idx = fetch_rakuten._build_jan_to_asin(root)
            self.assertEqual(len(idx), 1)
            self.assertIn(idx["4904810000000"], ("B0001", "B0002"))

    def test_returns_empty_when_root_missing(self):
        self.assertEqual(
            fetch_rakuten._build_jan_to_asin(pathlib.Path("/no/such/dir")),
            {},
        )


class JanExtractionTest(unittest.TestCase):
    def test_extracts_13_digit_jan(self):
        self.assertEqual(
            fetch_rakuten._extract_jan_from_text("商品コード 4904810642602 ご確認"),
            "4904810642602",
        )

    def test_extracts_8_digit_jan(self):
        self.assertEqual(
            fetch_rakuten._extract_jan_from_text("JAN: 49048102"),
            "49048102",
        )

    def test_does_not_match_year_like_numbers(self):
        # 2024 や 12345 は先頭 4 + 桁数で除外される (4 桁は 8/13 桁外)
        self.assertEqual(fetch_rakuten._extract_jan_from_text("2026 年最新"), "")
        self.assertEqual(fetch_rakuten._extract_jan_from_text("価格 12345 円"), "")

    def test_handles_empty(self):
        self.assertEqual(fetch_rakuten._extract_jan_from_text(""), "")
        self.assertEqual(fetch_rakuten._extract_jan_from_text(None), "")

    def test_does_not_match_within_longer_digit_string(self):
        # 14 桁以上の連続数字内では境界条件で取らない (前後数字 lookaround)
        self.assertEqual(
            fetch_rakuten._extract_jan_from_text("ID 49048106426029999"),
            "",
        )


class MatchRankingItemTest(unittest.TestCase):
    def setUp(self):
        self.itemcode_idx = {"shopa:123": "B0001", "shopb:456": "B0002"}
        self.jan_idx = {"4904810642602": "B0003"}

    def test_stage1_direct_itemcode_match(self):
        item = {"itemCode": "shopa:123", "title": "...", "itemCaption": ""}
        asin, stage = fetch_rakuten._match_ranking_item(item, self.itemcode_idx, self.jan_idx)
        self.assertEqual((asin, stage), ("B0001", "stage1"))

    def test_stage2_jan_via_caption(self):
        item = {
            "itemCode": "newshop:xyz",
            "title": "知育ブロック",
            "itemCaption": "JAN 4904810642602 商品説明",
        }
        asin, stage = fetch_rakuten._match_ranking_item(item, self.itemcode_idx, self.jan_idx)
        self.assertEqual((asin, stage), ("B0003", "stage2_jan"))

    def test_stage2_jan_via_title(self):
        item = {
            "itemCode": "newshop:xyz",
            "title": "知育ブロック 4904810642602",
            "itemCaption": "",
        }
        asin, stage = fetch_rakuten._match_ranking_item(item, self.itemcode_idx, self.jan_idx)
        self.assertEqual((asin, stage), ("B0003", "stage2_jan"))

    def test_unmatched_returns_empty(self):
        item = {"itemCode": "unknown:shop", "title": "未知の商品", "itemCaption": "説明文"}
        asin, stage = fetch_rakuten._match_ranking_item(item, self.itemcode_idx, self.jan_idx)
        self.assertEqual((asin, stage), ("", ""))

    def test_stage1_takes_precedence_over_jan(self):
        # itemCode が既知なら caption の JAN は見ない
        item = {
            "itemCode": "shopa:123",
            "title": "...",
            "itemCaption": "JAN 4904810642602",
        }
        asin, stage = fetch_rakuten._match_ranking_item(item, self.itemcode_idx, self.jan_idx)
        self.assertEqual((asin, stage), ("B0001", "stage1"))


if __name__ == "__main__":
    unittest.main()
