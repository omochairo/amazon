"""Unit tests for score_calculator.compute_ivs_axes (#589).

`compute_ivs_axes` projects the 7-element internal breakdown to the 4 axes
shown in the /5 score UI (article page + list-card mini-chart).
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import json  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

import internal_links  # noqa: E402
import score_calculator  # noqa: E402
from brand_normalizer import normalize as normalize_brand  # noqa: E402
from score_calculator import (  # noqa: E402
    calculate as calculate_score,
    compute_ivs_axes,
    get_media_exposure_metrics,
    reset_media_exposure_metrics,
)


def _bd(**kwargs):
    base = {"brand_tier": 0, "safety_cert": 0, "age_fit": 0, "edu_value": 0,
            "media_exposure": 0, "multi_market": 0, "price_value": 0}
    base.update(kwargs)
    return base


class ComputeIvsAxesTest(unittest.TestCase):
    def test_returns_all_four_axes(self):
        axes = compute_ivs_axes(_bd())
        self.assertEqual(set(axes), {"education", "safety", "cost_performance", "longevity"})

    def test_zero_breakdown_floor_2(self):
        # 玩具は 0/5 にならない設計: raw 0 -> 2.0 floor
        axes = compute_ivs_axes(_bd())
        for v in axes.values():
            self.assertEqual(v, 2.0)

    def test_max_breakdown_returns_5(self):
        axes = compute_ivs_axes(_bd(
            brand_tier=25, safety_cert=10, edu_value=15,
            media_exposure=15, price_value=15,
        ))
        self.assertEqual(axes["education"], 5.0)
        self.assertEqual(axes["safety"], 5.0)
        self.assertEqual(axes["cost_performance"], 5.0)
        self.assertEqual(axes["longevity"], 5.0)

    def test_mid_breakdown_returns_mid(self):
        # raw が max の半分 -> 中央 3.5
        axes = compute_ivs_axes(_bd(
            edu_value=7,           # 約 7/15 = 0.47 → 2 + 0.47*3 = 3.4
            safety_cert=5,         # 5/10 = 0.5 → 3.5
            price_value=7,         # 同上 3.4
            brand_tier=12,         # 12/25 = 0.48
            media_exposure=7,      # 7/15 = 0.47 → longevity ~ 3.42
        ))
        self.assertAlmostEqual(axes["safety"], 3.5, places=1)
        # longevity = 2 + ((12/25 + 7/15) / 2) * 3
        # = 2 + ((0.48 + 0.4667) / 2) * 3 = 2 + 0.4733 * 3 = 3.42
        self.assertAlmostEqual(axes["longevity"], 3.4, places=1)

    def test_independent_axes(self):
        # education と safety を一方だけ振り切る → もう一方は floor 据え置き
        axes = compute_ivs_axes(_bd(edu_value=15))
        self.assertEqual(axes["education"], 5.0)
        self.assertEqual(axes["safety"], 2.0)
        self.assertEqual(axes["cost_performance"], 2.0)


class OmchaThresholdTest(unittest.TestCase):
    """Issue #6103: iro/v2 の 0..100 スケールに引き直した閾値を固定する。

    v1 の 30/15/10 を持ち込むと media_exposure の配点が変わる。実タグ
    キーワード 250 件での分布合わせで 18/12 を選んだ。
    """

    def setUp(self):
        reset_media_exposure_metrics()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = pathlib.Path(self.tmp.name)
        self.asin_dir = self.repo_root / "data" / "raw" / "per_asin" / "B0TEST00001"
        self.asin_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()
        reset_media_exposure_metrics()

    def _me(self, scores):
        """omcha キャッシュだけ置いた状態の media_exposure 点を返す。"""
        (self.asin_dir / "omcha_related.json").write_text(
            json.dumps({"items": [{"score": s} for s in scores]}), encoding="utf-8"
        )
        article = {"product": {"brand": "Test", "asin": "B0TEST00001"}}
        res = calculate_score(
            article, normalize_brand("Test"), asin="B0TEST00001",
            repo_root=self.repo_root,
        )
        return res.breakdown["media_exposure"]

    def test_thresholds_are_v2_scale(self):
        self.assertEqual(score_calculator._OM_TOP_STRONG, 18)
        self.assertEqual(score_calculator._OM_TOP_MATCH,
                         internal_links.DEFAULT_MIN_SCORE)

    def test_strong_match_scores_4(self):
        self.assertEqual(self._me([18, 12]), 4)
        self.assertEqual(self._me([62]), 4)

    def test_weak_match_scores_3(self):
        self.assertEqual(self._me([12]), 3)
        self.assertEqual(self._me([17, 12]), 3)

    def test_below_min_score_scores_0(self):
        # fetch 側が min_score=12 で足切りするのでこの状態は起きないが、
        # 古いキャッシュが残っていても 0 点に倒れることを固定する。
        self.assertEqual(self._me([11]), 0)
        self.assertEqual(self._me([]), 0)

    def test_v1_thresholds_would_have_differed(self):
        # v1 の 30 をそのまま使っていたら 18..29 が 4 点にならなかった。
        self.assertEqual(self._me([25]), 4)


class MediaExposureMetricsTest(unittest.TestCase):
    """Issue #677: missing (file 不在) と empty (file 有り 0 hit) を分離記録。"""

    def setUp(self):
        reset_media_exposure_metrics()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = pathlib.Path(self.tmp.name)
        self.asin_dir = self.repo_root / "data" / "raw" / "per_asin" / "B0TEST00001"
        self.asin_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()
        reset_media_exposure_metrics()

    def _calc(self, asin="B0TEST00001"):
        article = {"product": {"brand": "Test", "asin": asin}}
        return calculate_score(
            article, normalize_brand("Test"), asin=asin, repo_root=self.repo_root
        )

    def test_all_missing(self):
        self._calc()
        m = get_media_exposure_metrics()
        self.assertEqual(m.total, 1)
        self.assertEqual(m.yt_missing, 1)
        self.assertEqual(m.news_missing, 1)
        self.assertEqual(m.omcha_missing, 1)
        self.assertEqual(m.yt_empty, 0)
        self.assertEqual(m.news_empty, 0)
        self.assertEqual(m.omcha_empty, 0)

    def test_present_files_with_empty_items(self):
        (self.asin_dir / "youtube.json").write_text('{"items": []}', encoding="utf-8")
        (self.asin_dir / "news.json").write_text('{"items": []}', encoding="utf-8")
        (self.asin_dir / "omcha_related.json").write_text('{"items": []}', encoding="utf-8")
        self._calc()
        m = get_media_exposure_metrics()
        self.assertEqual(m.total, 1)
        self.assertEqual(m.yt_empty, 1)
        self.assertEqual(m.news_empty, 1)
        self.assertEqual(m.omcha_empty, 1)
        self.assertEqual(m.yt_missing, 0)
        self.assertEqual(m.news_missing, 0)
        self.assertEqual(m.omcha_missing, 0)

    def test_present_files_with_items_neither_missing_nor_empty(self):
        (self.asin_dir / "youtube.json").write_text(
            json.dumps({"items": [{"url": "x", "title": "t"}]}), encoding="utf-8"
        )
        (self.asin_dir / "news.json").write_text(
            json.dumps({"items": [{"title": "n"}]}), encoding="utf-8"
        )
        (self.asin_dir / "omcha_related.json").write_text(
            json.dumps({"items": [{"score": 20}]}), encoding="utf-8"
        )
        self._calc()
        m = get_media_exposure_metrics()
        self.assertEqual(m.total, 1)
        self.assertEqual(m.yt_missing + m.yt_empty, 0)
        self.assertEqual(m.news_missing + m.news_empty, 0)
        self.assertEqual(m.omcha_missing + m.omcha_empty, 0)

    def test_metrics_accumulate_across_calls(self):
        self._calc("B0TEST00001")
        (self.repo_root / "data" / "raw" / "per_asin" / "B0TEST00002").mkdir()
        self._calc("B0TEST00002")
        m = get_media_exposure_metrics()
        self.assertEqual(m.total, 2)

    def test_reset_clears_counters(self):
        self._calc()
        reset_media_exposure_metrics()
        m = get_media_exposure_metrics()
        self.assertEqual(m.total, 0)
        self.assertEqual(m.yt_missing, 0)

    def test_as_dict_contains_all_keys(self):
        d = get_media_exposure_metrics().as_dict()
        expected = {
            "total", "yt_missing", "yt_empty",
            "news_missing", "news_empty",
            "omcha_missing", "omcha_empty",
        }
        self.assertEqual(set(d), expected)


if __name__ == "__main__":
    unittest.main()
