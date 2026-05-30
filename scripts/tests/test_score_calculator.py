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

from score_calculator import compute_ivs_axes  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
