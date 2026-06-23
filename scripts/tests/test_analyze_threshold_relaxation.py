"""Issue #1072 Option A dry-run analyzer のユニットテスト。

カバレッジ:
1. _passes / _why_fail: build_post 既存閾値の挙動を 1:1 で再現していること
2. preset 緩和: 価格帯外 / coverage 不足の fail を救済できること
3. analyze: rescued カウンタが current で fail → relax で pass の差分を正しく拾う
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

import analyze_threshold_relaxation as atr  # noqa: E402
import build_post  # noqa: E402


class PassesGateTest(unittest.TestCase):
    def setUp(self):
        self.current = atr.ThresholdPreset.current()

    def test_passes_exact_match(self):
        matched = {
            "title": "ハズブロ ナーフ エリート 2.0 コマンダー RD-6",
            "price": 2300,
            "search_keyword": "ハズブロ ナーフ RD-6",
        }
        self.assertTrue(atr._passes(matched, 2302, self.current))

    def test_fails_price_too_high(self):
        matched = {
            "title": "ハズブロ ナーフ エリート 2.0 コマンダー RD-6",
            "price": 6000,  # 2302 * 2.5 = 5755 超
            "search_keyword": "ハズブロ ナーフ RD-6",
        }
        self.assertFalse(atr._passes(matched, 2302, self.current))
        self.assertIn("price_too_high", atr._why_fail(matched, 2302, self.current))

    def test_fails_price_too_low(self):
        matched = {
            "title": "ハズブロ ナーフ エリート 2.0 コマンダー RD-6",
            "price": 800,  # 2302 * 0.4 = 920 未満
            "search_keyword": "ハズブロ ナーフ RD-6",
        }
        self.assertFalse(atr._passes(matched, 2302, self.current))
        self.assertIn("price_too_low", atr._why_fail(matched, 2302, self.current))

    def test_fails_coverage_low(self):
        """5 meaningful tokens で 2 hit = cov 0.40 < 0.5 で fail。

        先頭 token (コマンダー) は title 先頭 (ハズブロ) と不一致なので
        descriptor (#1140) ガードは発火せず、純粋に coverage で落ちる。
        """
        matched = {
            "title": "ハズブロ ナーフ",  # コマンダー/アルファ/ストライク 抜け
            "price": 2300,
            "search_keyword": "コマンダー ハズブロ ナーフ アルファ ストライク",
        }
        self.assertFalse(atr._passes(matched, 2302, self.current))
        reason = atr._why_fail(matched, 2302, self.current)
        self.assertIn("coverage_low", reason)

    def test_no_meaningful_tokens_passes_through(self):
        matched = {
            "title": "知育玩具 おもちゃ プレゼント",
            "price": 1500,
            "search_keyword": "おもちゃ 知育玩具",  # 全て generic
        }
        # generic のみ → median band 選出を尊重して true
        self.assertTrue(atr._passes(matched, 1500, self.current))

    def test_empty_title_fails(self):
        self.assertFalse(atr._passes({"title": "", "price": 100}, 100, self.current))
        self.assertFalse(atr._passes({"title": "x", "price": 0}, 100, self.current))


class RelaxRescueTest(unittest.TestCase):
    """current で fail → relax で pass する救済条件のテスト。"""

    def test_relax_coverage_rescues_low_coverage(self):
        matched = {
            "title": "ハズブロ ナーフ",
            "price": 2300,
            "search_keyword": "コマンダー ハズブロ ナーフ アルファ ストライク",
        }
        presets = atr._build_presets()
        # current (cov 0.5) は cov 0.40 で fail
        self.assertFalse(atr._passes(matched, 2302, presets[0]))
        # relax_coverage (0.34) は pass
        relax_cov = next(p for p in presets if p.name == "relax_coverage")
        self.assertTrue(atr._passes(matched, 2302, relax_cov))

    def test_relax_price_rescues_high_ratio(self):
        matched = {
            "title": "ロンビー 自分でつくる 屋内遊具",
            "price": 8100,  # amazon 3000 → ratio 2.70 (current 2.5 を超える)
            "search_keyword": "ロンビー 自分でつくる 屋内遊具",
        }
        presets = atr._build_presets()
        self.assertFalse(atr._passes(matched, 3000, presets[0]))
        relax_price = next(p for p in presets if p.name == "relax_price")
        self.assertTrue(atr._passes(matched, 3000, relax_price))  # 3.0x まで許容


class AnalyzeAggregationTest(unittest.TestCase):
    def test_summary_counts_rescued_correctly(self):
        """rakuten では救済される / yahoo では既に pass のケース。"""
        amazon_raw = {"items": [
            {"asin": "B00TEST01", "price": 2302},
            {"asin": "B00TEST02", "price": 1500},
        ]}
        # B00TEST01: rakuten cov 0.40 で current fail / relax_coverage(0.34) で pass
        # B00TEST02: rakuten で既に pass
        rakuten_index = {
            "B00TEST01": {
                "matched_asin": "B00TEST01",
                "title": "ハズブロ ナーフ",
                "price": 2300,
                "search_keyword": "コマンダー ハズブロ ナーフ アルファ ストライク",
            },
            "B00TEST02": {
                "matched_asin": "B00TEST02",
                "title": "テスト商品 完全一致 KW",
                "price": 1500,
                "search_keyword": "テスト商品 完全一致 KW",
            },
        }
        yahoo_index = {}
        presets = atr._build_presets()
        reports, summary = atr.analyze(
            ["B00TEST01", "B00TEST02"],
            rakuten_index, yahoo_index, amazon_raw,
            pathlib.Path("/nonexistent"), presets,
        )
        self.assertEqual(len(reports), 2)
        # current では 1 pass (B00TEST02 のみ)
        self.assertEqual(summary["current"]["rakuten_pass"], 1)
        self.assertEqual(summary["current"]["rakuten_rescued"], 0)
        # relax_coverage では 2 pass (B00TEST01 rescued)
        self.assertEqual(summary["relax_coverage"]["rakuten_pass"], 2)
        self.assertEqual(summary["relax_coverage"]["rakuten_rescued"], 1)

    def test_no_candidate_does_not_count(self):
        """matched.json に entry が無い ASIN は pass にも rescued にも数えない。"""
        presets = atr._build_presets()
        reports, summary = atr.analyze(
            ["B00MISSING"],
            {}, {}, {}, pathlib.Path("/nonexistent"), presets,
        )
        self.assertEqual(len(reports), 1)
        self.assertFalse(reports[0].rakuten["has_candidate"])
        for p in presets:
            self.assertEqual(summary[p.name]["rakuten_pass"], 0)
            self.assertEqual(summary[p.name]["rakuten_rescued"], 0)


class GateDriftRegressionTest(unittest.TestCase):
    """build_post._matched_passes_quality と _passes(current preset) が
    同じ判定を返すことを既知ケースで確認。drift 検知用。

    _passes は本番ゲートを委譲呼びするので原理上一致するが、将来
    シグネチャ変更等で乖離したら検知できるよう回帰として残す (#2723)。"""

    def test_drift_with_known_cases(self):
        current = atr.ThresholdPreset.current()
        cases = [
            (  # full match: 通過
                {"title": "テスト 商品 KW", "price": 1500, "search_keyword": "テスト 商品 KW"},
                1500,
            ),
            (  # price too high (ratio 3.3 > 2.5): fail
                {"title": "テスト 商品 KW", "price": 5000, "search_keyword": "テスト 商品 KW"},
                1500,
            ),
            (  # descriptor (#1140) ガード: KW/XX が title 不在で fail
                {"title": "テスト 商品", "price": 1500, "search_keyword": "テスト 商品 KW XX"},
                1500,
            ),
        ]
        for matched, amazon_price in cases:
            self.assertEqual(
                atr._passes(matched, amazon_price, current),
                build_post._matched_passes_quality(matched, amazon_price),
                msg=f"drift on matched={matched}",
            )


if __name__ == "__main__":
    unittest.main()
