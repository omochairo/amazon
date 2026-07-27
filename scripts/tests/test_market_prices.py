"""Unit tests for market_prices.py (#4007 follow-up 1).

Coverage:
1. resolve_price   - matched が quality gate を通過したら matched price を採用、
                      通過しなければ existing を維持 (extreme outlier は破棄)。
2. load_matched_index - matched_asin 優先 / asin フォールバック / ファイル不在 /
                      壊れた JSON。

build_post.py 側の _MARKET_* 定数・_matched_passes_quality の値との一致は
scripts/tests/test_market_quality_gate.py が既に固定しているため、本ファイルは
そちらを編集せずに pass することで market_prices への移設エイリアスが効いている
ことの証拠にもなる。
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

import market_prices  # noqa: E402


# ---------------------------------------------------------------------------
# resolve_price
# ---------------------------------------------------------------------------

class ResolvePriceTest(unittest.TestCase):
    def test_matched_passing_quality_gate_wins(self):
        """matched が gate を通れば matched price を採用する (Jules 値を上書き)。"""
        matched = {"title": "知育玩具 木製パズル", "price": 2500, "search_keyword": ""}
        price = market_prices.resolve_price(
            existing_price=4000, matched=matched, amazon_price=2800,
        )
        self.assertEqual(price, 2500)

    def test_matched_missing_keeps_existing(self):
        """matched が無ければ既存値をそのまま維持する。"""
        price = market_prices.resolve_price(
            existing_price=3200, matched=None, amazon_price=3000,
        )
        self.assertEqual(price, 3200)

    def test_matched_fails_quality_gate_keeps_existing(self):
        """matched はあるが gate 落ち (price band 外) → 既存値を維持する。"""
        # amazon=2302 の band は [920.8, 5755]。399 は 0.4x 未満で gate 落ち。
        matched = {"title": "ハズブロ ナーフ エリート 2.0 コマンダー", "price": 399,
                   "search_keyword": "ハズブロ ナーフ コマンダー"}
        price = market_prices.resolve_price(
            existing_price=2200, matched=matched, amazon_price=2302,
        )
        self.assertEqual(price, 2200)

    def test_existing_extreme_outlier_dropped_when_matched_fails(self):
        """matched 無し/gate 落ち かつ existing が Amazon の 3.0x 超 → 0 (最安候補から除外)。

        #4007 follow-up 1 実測の実例 (B0F4X462WH 相当): amazon 1579円に対し
        yahoo_matched 側の旧値 14395円 (9.12x) は別商品確定として丸ごと破棄する。
        """
        price = market_prices.resolve_price(
            existing_price=14395, matched=None, amazon_price=1579,
        )
        self.assertEqual(price, 0)

    def test_existing_extreme_low_outlier_dropped(self):
        """existing が Amazon の 1/3 未満の極端な安値も破棄する。"""
        price = market_prices.resolve_price(
            existing_price=300, matched=None, amazon_price=2000,
        )
        self.assertEqual(price, 0)

    def test_existing_not_extreme_survives_without_amazon_anchor(self):
        """amazon_price=0 (Amazon 取り扱い無し) では extreme 判定自体が働かず existing を維持。"""
        price = market_prices.resolve_price(
            existing_price=5000, matched=None, amazon_price=0,
        )
        self.assertEqual(price, 5000)

    def test_no_matched_no_existing_returns_zero(self):
        price = market_prices.resolve_price(
            existing_price=0, matched=None, amazon_price=2000,
        )
        self.assertEqual(price, 0)

    def test_matched_price_unparseable_treated_as_zero(self):
        """matched.price が壊れていても quality gate 自体を通らない (price<=0 で False)
        ので existing にフォールバックする。"""
        matched = {"title": "テスト商品", "price": "not-a-number", "search_keyword": ""}
        price = market_prices.resolve_price(
            existing_price=1800, matched=matched, amazon_price=2000,
        )
        self.assertEqual(price, 1800)


# ---------------------------------------------------------------------------
# load_matched_index
# ---------------------------------------------------------------------------

class LoadMatchedIndexTest(unittest.TestCase):
    def test_prefers_matched_asin_over_asin(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "rakuten_matched.json"
            path.write_text(json.dumps({
                "items": [
                    {"asin": "B0FALLBACK", "matched_asin": "B0PRIMARY", "price": 1000},
                ]
            }), encoding="utf-8")
            index = market_prices.load_matched_index(path)
            self.assertIn("B0PRIMARY", index)
            self.assertNotIn("B0FALLBACK", index)

    def test_falls_back_to_asin_when_matched_asin_absent(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "yahoo_matched.json"
            path.write_text(json.dumps({
                "items": [{"asin": "B0PLAINASIN", "price": 2000}]
            }), encoding="utf-8")
            index = market_prices.load_matched_index(path)
            self.assertIn("B0PLAINASIN", index)

    def test_missing_file_returns_empty_dict(self):
        path = pathlib.Path("/does/not/exist/rakuten_matched.json")
        self.assertEqual(market_prices.load_matched_index(path), {})

    def test_corrupted_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "rakuten_matched.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(market_prices.load_matched_index(path), {})

    def test_items_without_asin_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "rakuten_matched.json"
            path.write_text(json.dumps({
                "items": [
                    {"price": 1000},  # no asin/matched_asin
                    "not-a-dict",
                    {"asin": "B0KEEP", "price": 500},
                ]
            }), encoding="utf-8")
            index = market_prices.load_matched_index(path)
            self.assertEqual(set(index.keys()), {"B0KEEP"})


if __name__ == "__main__":
    unittest.main()
