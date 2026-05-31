"""build_post._matched_passes_quality の閾値ガードのユニットテスト。

Issue #1072 Phase 3-B (2026-05-31): jan_unknown 58 ASIN を救済するため
price band を [0.5, 2.0] → [0.4, 2.5], coverage を 0.7 → 0.5 に緩和した。
dry-run (scripts/analyze_threshold_relaxation.py) の救済候補 25 件のうち
代表ケースをテストで pin して、将来の閾値 drift を検知する。
"""

from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_post  # noqa: E402


class MarketQualityGateConstantsTest(unittest.TestCase):
    """Pin the relaxed preset (#1072 Phase 3-B)."""

    def test_price_band_low(self):
        self.assertEqual(build_post._MARKET_PRICE_BAND_LOW, 0.4)

    def test_price_band_high(self):
        self.assertEqual(build_post._MARKET_PRICE_BAND_HIGH, 2.5)

    def test_coverage_ratio(self):
        self.assertEqual(build_post._MARKET_COVERAGE_RATIO, 0.5)


class MatchedPassesQualityTest(unittest.TestCase):
    """Real-world cases from scratch/threshold_relaxation.md."""

    def _check(self, *, title: str, price: int, kw: str, amazon_price: int) -> bool:
        return build_post._matched_passes_quality(
            {"title": title, "price": price, "search_keyword": kw},
            amazon_price,
        )

    # === relax_both で救済される代表ケース (dry-run で確認済) ===

    def test_coverage_050_rescued(self):
        """B097GY576F: WYSWYG 240ピース、cov=0.50 (旧 0.7 で fail)。"""
        self.assertTrue(self._check(
            title="【送料無料】WYSWYG 240ピース創造大きいブロックセット レゴ デュプロ互換",
            price=4899,
            kw="WYSWYG 240ピース創造大きいブロックセット 積み木 -デュプロ/アンパン",
            amazon_price=3041,
        ))

    def test_coverage_067_rescued(self):
        """B09BQMCSFL: ハズブロ ナーフ RD-6、cov=0.67 (旧 0.7 で fail)。"""
        self.assertTrue(self._check(
            title="ハズブロ ナーフ エリート 2.0 コマンダー RD−6",
            price=1775,
            kw="ハズブロ ナーフ RD-6",
            amazon_price=2302,
        ))

    def test_price_ratio_210_rescued(self):
        """B0875FV2BQ: ロンビー中古プレミア、ratio=2.10 (旧 2.0 で fail)。"""
        self.assertTrue(self._check(
            title="【中古】トイオブザイヤー2025受賞 ロンビー (Lon-Bi) 自分でつくる屋内遊具",
            price=6613,
            kw="トイオブザイヤー2025受賞 ロンビー 自分でつくる屋内遊具 室内遊び",
            amazon_price=3149,
        ))

    # === aggressive only / 旧来通り fail させ続けたい境界 ===

    def test_price_ratio_275_still_rejected(self):
        """B0GFVV4YG9: ratio=2.75 は relax_aggressive のみで pass、本実装では fail のまま。"""
        self.assertFalse(self._check(
            title="Mamimami Home アクティビティキューブ 木製 型はめ モンテッソーリ",
            price=3962,
            kw="Mamimami Home ミニカー 木製",
            amazon_price=1439,
        ))

    def test_price_below_band_rejected(self):
        """0.4x 未満は除外。"""
        self.assertFalse(self._check(
            title="ハズブロ ナーフ エリート 2.0 コマンダー",
            price=399,
            kw="ハズブロ ナーフ コマンダー",
            amazon_price=2302,
        ))

    def test_no_meaningful_tokens_passes(self):
        """汎用語のみの search_keyword は cross-search 側 median band 選出を尊重して pass。"""
        self.assertTrue(self._check(
            title="知育玩具 木製パズル",
            price=2000,
            kw="おもちゃ 知育玩具 木製",  # 全部 _MARKET_GENERIC_TOKENS
            amazon_price=1800,
        ))


if __name__ == "__main__":
    unittest.main()
