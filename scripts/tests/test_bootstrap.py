"""scripts/experimental/multistage_brief/bootstrap.py unit tests (#4841 M1-c R1)。"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.bootstrap import (
    bootstrap_mean_ci,
    ci_excludes_zero_on_positive_side,
)


class BootstrapMeanCiTest(unittest.TestCase):
    def test_deterministic_with_fixed_seed(self):
        values = [12, 1, 11, 4, 10, 3, 2, 3, -4, 2, -1, -2, 6, -1, 6]
        a = bootstrap_mean_ci(values, seed=1, n_resamples=2000)
        b = bootstrap_mean_ci(values, seed=1, n_resamples=2000)
        self.assertEqual(a, b)

    def test_different_seed_gives_same_mean_but_independent_resampling(self):
        values = [12, 1, 11, 4, 10, 3, 2, 3, -4, 2, -1, -2, 6, -1, 6]
        a = bootstrap_mean_ci(values, seed=1, n_resamples=2000)
        b = bootstrap_mean_ci(values, seed=2, n_resamples=2000)
        # 平均そのものは seed に依存しない (元データから決まる)
        self.assertEqual(a["mean"], b["mean"])
        # 信頼区間の下限は元データの分散内に収まる
        self.assertGreater(a["lower"], min(values))
        self.assertGreater(b["lower"], min(values))

    def test_mean_matches_sample_mean(self):
        values = [1, 2, 3, 4, 5]
        result = bootstrap_mean_ci(values, seed=1, n_resamples=2000)
        self.assertEqual(result["mean"], 3.0)

    def test_ci_excludes_zero_when_all_values_strongly_positive(self):
        values = [10, 11, 9, 10, 12, 9, 11, 10] * 3
        result = bootstrap_mean_ci(values, seed=1, n_resamples=5000)
        self.assertGreater(result["lower"], 0)
        self.assertTrue(ci_excludes_zero_on_positive_side(result))

    def test_ci_straddles_zero_when_values_are_mixed_and_noisy(self):
        values = [5, -5, 4, -4, 3, -3, 6, -6, 1, -1]
        result = bootstrap_mean_ci(values, seed=1, n_resamples=5000)
        self.assertLessEqual(result["lower"], 0)
        self.assertFalse(ci_excludes_zero_on_positive_side(result))

    def test_fewer_than_two_values_returns_none_bounds(self):
        result = bootstrap_mean_ci([5], seed=1)
        self.assertEqual(result["mean"], 5)
        self.assertIsNone(result["lower"])
        self.assertIsNone(result["upper"])
        self.assertFalse(ci_excludes_zero_on_positive_side(result))

    def test_empty_values_returns_none_mean_and_bounds(self):
        result = bootstrap_mean_ci([], seed=1)
        self.assertIsNone(result["mean"])
        self.assertIsNone(result["lower"])
        self.assertIsNone(result["upper"])
        self.assertEqual(result["n"], 0)


class CiExcludesZeroOnPositiveSideTest(unittest.TestCase):
    def test_none_lower_is_not_excluded(self):
        self.assertFalse(ci_excludes_zero_on_positive_side({"lower": None}))

    def test_negative_lower_is_not_excluded(self):
        self.assertFalse(ci_excludes_zero_on_positive_side({"lower": -0.1}))

    def test_zero_lower_is_not_excluded(self):
        self.assertFalse(ci_excludes_zero_on_positive_side({"lower": 0}))

    def test_positive_lower_is_excluded(self):
        self.assertTrue(ci_excludes_zero_on_positive_side({"lower": 0.01}))


if __name__ == "__main__":
    unittest.main()
