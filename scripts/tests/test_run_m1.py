"""scripts/experimental/multistage_brief/run_m1.py の pure function 単体テスト (#4841 M1)。

IO を伴う find_valid_rewrite_pairs / run() は対象外 (run_experiment.py の
process_asin/run と同じ方針。summarize 相当の集計・サンプリングロジックのみ検証)。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.run_m1 import entailment_spot_check_sample, select_validation_pairs


class SelectValidationPairsTest(unittest.TestCase):
    def test_returns_all_when_fewer_than_n(self):
        pairs = [{"asin": "B0000000AA"}, {"asin": "B0000000BB"}]
        result = select_validation_pairs(pairs, n=15)
        self.assertEqual([p["asin"] for p in result], ["B0000000AA", "B0000000BB"])

    def test_samples_deterministically_with_fixed_seed(self):
        pairs = [{"asin": f"B00000000{i}"} for i in range(20)]
        a = select_validation_pairs(pairs, n=5, seed=1)
        b = select_validation_pairs(pairs, n=5, seed=1)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 5)
        # 結果は asin でソートされている
        self.assertEqual([p["asin"] for p in a], sorted(p["asin"] for p in a))

    def test_different_seed_can_change_selection(self):
        pairs = [{"asin": f"B00000000{i}"} for i in range(20)]
        a = select_validation_pairs(pairs, n=5, seed=1)
        b = select_validation_pairs(pairs, n=5, seed=2)
        self.assertNotEqual([p["asin"] for p in a], [p["asin"] for p in b])

    def test_n_zero_or_negative_returns_all_without_sampling(self):
        pairs = [{"asin": f"B00000000{i}"} for i in range(20)]
        result = select_validation_pairs(pairs, n=0)
        self.assertEqual(len(result), 20)
        self.assertEqual([p["asin"] for p in result], sorted(p["asin"] for p in pairs))


class EntailmentSpotCheckSampleTest(unittest.TestCase):
    def test_only_judged_records_are_eligible(self):
        records = [
            {"sentence": "s1", "supported": True},
            {"sentence": "s2", "supported": False},
            {"sentence": "s3", "supported": None},
        ]
        result = entailment_spot_check_sample(records, n=10, seed=1)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["supported"] is not None for r in result))

    def test_caps_at_n(self):
        records = [{"sentence": f"s{i}", "supported": True} for i in range(30)]
        result = entailment_spot_check_sample(records, n=20, seed=1)
        self.assertEqual(len(result), 20)

    def test_returns_all_when_fewer_than_n(self):
        records = [{"sentence": "s1", "supported": True}]
        result = entailment_spot_check_sample(records, n=20, seed=1)
        self.assertEqual(result, records)


if __name__ == "__main__":
    unittest.main()
