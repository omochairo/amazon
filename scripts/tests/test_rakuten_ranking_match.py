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


class ArticleAsinIndexTest(unittest.TestCase):
    """Issue #1149: _build_article_asins / 記事ありき gate のテスト。"""

    def test_extracts_asin_from_post_filenames(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for name in [
                "2026-05-31-B0FRMFZW29.md",  # 正規
                "2026-05-30-b076zdqb15.md",  # 小文字でも拾う
                "2026-01-01-B0XXXXXXXX.md",  # 別 ASIN
                "_index.md",                 # 拾わない
                "draft-note.md",             # 拾わない
            ]:
                (root / name).write_text("---\n---\n", encoding="utf-8")
            asins = fetch_rakuten._build_article_asins(root)
            self.assertEqual(asins, {"B0FRMFZW29", "B076ZDQB15", "B0XXXXXXXX"})

    def test_returns_empty_when_missing(self):
        self.assertEqual(
            fetch_rakuten._build_article_asins(pathlib.Path("/no/such/posts")),
            set(),
        )

    def test_match_filters_out_asin_without_article(self):
        itemcode_idx = {"shopa:1": "B0001"}
        jan_idx = {"4904810642602": "B0002"}
        article_asins = {"B0002"}  # B0001 は記事無し
        # itemCode は当たるが記事無し → unmatched
        item1 = {"itemCode": "shopa:1", "title": "x", "itemCaption": ""}
        self.assertEqual(
            fetch_rakuten._match_ranking_item(item1, itemcode_idx, jan_idx, article_asins),
            ("", ""),
        )
        # JAN は当たり記事あり → stage2_jan
        item2 = {"itemCode": "", "title": "x", "itemCaption": "JAN 4904810642602"}
        self.assertEqual(
            fetch_rakuten._match_ranking_item(item2, itemcode_idx, jan_idx, article_asins),
            ("B0002", "stage2_jan"),
        )

    def test_match_without_article_filter_is_backward_compatible(self):
        # article_asins=None なら従来通り全マッチ許容
        itemcode_idx = {"shopa:1": "B0001"}
        jan_idx = {}
        item = {"itemCode": "shopa:1", "title": "x", "itemCaption": ""}
        self.assertEqual(
            fetch_rakuten._match_ranking_item(item, itemcode_idx, jan_idx, None),
            ("B0001", "stage1"),
        )


class RematchOnlyTest(unittest.TestCase):
    """Issue #1149: --rematch-only モードが既存 weekly.json を現行 indices で再マッチし、
    記事の無い ASIN は unmatched 扱いにすることを検証する。"""

    def test_rematch_links_newly_added_article(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "data/raw/per_asin/B0NEW00001").mkdir(parents=True)
            (root / "data/raw/per_asin/B0NEW00001/amazon.json").write_text(
                json.dumps({"item": {"jan_code": "4904810000001"}}), encoding="utf-8"
            )
            (root / "data/raw/rakuten_matched.json").write_text(
                json.dumps({"items": []}), encoding="utf-8"
            )
            (root / "hugo/content/posts").mkdir(parents=True)
            (root / "hugo/content/posts/2026-05-31-B0NEW00001.md").write_text(
                "---\n---\n", encoding="utf-8"
            )
            weekly_dir = root / "hugo/data/ranking"
            weekly_dir.mkdir(parents=True)
            (weekly_dir / "weekly.json").write_text(json.dumps({
                "generated_at": "2026-05-25T00:00:00+00:00",
                "items": [
                    {"rank": 1, "title": "t", "itemCaption": "JAN 4904810000001",
                     "itemCode": "shop:1", "matched_asin": None, "match_stage": None},
                    {"rank": 2, "title": "t2", "itemCaption": "no jan",
                     "itemCode": "shop:2", "matched_asin": None, "match_stage": None},
                ],
            }), encoding="utf-8")

            cwd = os.getcwd()
            try:
                os.chdir(root)
                rc = fetch_rakuten._rematch_only(pathlib.Path("data/raw"))
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0)
            after = json.loads((weekly_dir / "weekly.json").read_text(encoding="utf-8"))
            self.assertEqual(after["items"][0]["matched_asin"], "B0NEW00001")
            self.assertEqual(after["items"][0]["match_stage"], "stage2_jan")
            self.assertIsNone(after["items"][1]["matched_asin"])
            self.assertIn("rematched_at", after)

    def test_rematch_skips_asin_without_article(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "data/raw/per_asin/B0NOART0001").mkdir(parents=True)
            (root / "data/raw/per_asin/B0NOART0001/amazon.json").write_text(
                json.dumps({"item": {"jan_code": "4904810000002"}}), encoding="utf-8"
            )
            (root / "data/raw/rakuten_matched.json").write_text(
                json.dumps({"items": []}), encoding="utf-8"
            )
            (root / "hugo/content/posts").mkdir(parents=True)  # 空 = 記事無し
            weekly_dir = root / "hugo/data/ranking"
            weekly_dir.mkdir(parents=True)
            (weekly_dir / "weekly.json").write_text(json.dumps({
                "items": [
                    {"rank": 1, "title": "t", "itemCaption": "JAN 4904810000002",
                     "itemCode": "shop:1", "matched_asin": None, "match_stage": None},
                ],
            }), encoding="utf-8")
            cwd = os.getcwd()
            try:
                os.chdir(root)
                rc = fetch_rakuten._rematch_only(pathlib.Path("data/raw"))
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0)
            after = json.loads((weekly_dir / "weekly.json").read_text(encoding="utf-8"))
            # per_asin にあっても記事が無いので /products/<asin>/ 404 防止で未マッチ
            self.assertIsNone(after["items"][0]["matched_asin"])

    def test_rematch_missing_weekly_returns_code_2(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            try:
                os.chdir(td)
                rc = fetch_rakuten._rematch_only(pathlib.Path("data/raw"))
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
