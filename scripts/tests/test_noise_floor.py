"""scripts/experimental/multistage_brief/noise_floor.py unit tests (#4841 M1-a)。"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.noise_floor import compute_noise_floor


class ComputeNoiseFloorTest(unittest.TestCase):
    def test_computes_per_asin_stdev_and_pairwise_diffs(self):
        runs = [
            {"asin": "A", "seed": 1, "max_sim": 0.90},
            {"asin": "A", "seed": 2, "max_sim": 0.92},
            {"asin": "A", "seed": 3, "max_sim": 0.94},
            {"asin": "B", "seed": 1, "max_sim": 0.80},
            {"asin": "B", "seed": 2, "max_sim": 0.80},
            {"asin": "B", "seed": 3, "max_sim": 0.80},
        ]
        result = compute_noise_floor(runs)
        self.assertEqual(result["asin_count"], 2)
        self.assertEqual(result["runs_per_asin"], {"A": 3, "B": 3})
        self.assertGreater(result["per_asin_stdev"]["A"], 0)
        self.assertEqual(result["per_asin_stdev"]["B"], 0.0)
        # A: |0.90-0.92|,|0.90-0.94|,|0.92-0.94| = 0.02,0.04,0.02  B: 0,0,0
        self.assertEqual(result["pairwise_diff_distribution"]["count"], 6)
        self.assertIsNotNone(result["floor"])

    def test_single_run_per_asin_has_none_stdev_and_no_pairs(self):
        runs = [{"asin": "A", "seed": 1, "max_sim": 0.9}]
        result = compute_noise_floor(runs)
        self.assertIsNone(result["per_asin_stdev"]["A"])
        self.assertEqual(result["pairwise_diff_distribution"]["count"], 0)
        self.assertIsNone(result["floor"])

    def test_ignores_missing_max_sim(self):
        runs = [
            {"asin": "A", "seed": 1, "max_sim": None},
            {"asin": "A", "seed": 2, "max_sim": 0.9},
        ]
        result = compute_noise_floor(runs)
        self.assertEqual(result["runs_per_asin"], {"A": 1})

    def test_metric_key_selects_a_different_field(self):
        runs = [
            {"asin": "A", "seed": 1, "unique_and_supported_count": 3},
            {"asin": "A", "seed": 2, "unique_and_supported_count": 5},
        ]
        result = compute_noise_floor(runs, metric_key="unique_and_supported_count")
        self.assertEqual(result["pairwise_diff_distribution"]["count"], 1)
        self.assertEqual(result["floor"], 2.0)

    def test_empty_runs(self):
        result = compute_noise_floor([])
        self.assertEqual(result["asin_count"], 0)
        self.assertIsNone(result["floor"])


if __name__ == "__main__":
    unittest.main()
