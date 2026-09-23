"""scripts/experimental/multistage_brief/run_p1_generator_ceiling.py の単体テスト (#4841 P1)。

IO (agy/gemma/Ruri を叩く process_asin/run) は対象外 (test_run_m2.py と同じ方針)。
compute_verdict (run_m2.compute_verdict と同一方式のブートストラップ判定) と
AgyCallBudget (呼び出し予算) を検証する。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.run_p1_generator_ceiling import AgyCallBudget, compute_verdict


def _pair(diff_count, diff_ratio, diff_unsupported):
    return {"diff_count": diff_count, "diff_ratio": diff_ratio, "diff_unsupported_count": diff_unsupported}


class ComputeVerdictTest(unittest.TestCase):
    def test_effective_when_count_and_ratio_positive_and_unsupported_not_increased(self):
        pairs = [_pair(3, 0.1, -1) for _ in range(10)]
        verdict = compute_verdict(pairs, generator="agy")
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertTrue(verdict["unsupported_not_increased"])
        self.assertTrue(verdict["effective"])
        self.assertIn("有効", verdict["verdict"])

    def test_ineffective_when_diffs_straddle_zero(self):
        pairs = [
            _pair(d, r, 0)
            for d, r in zip(
                [2, -2, 1, -1, 3, -3, 0, 2, -2, 1],
                [0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0, 0.1, -0.1, 0.05],
            )
        ]
        verdict = compute_verdict(pairs, generator="agy")
        self.assertFalse(verdict["effective"])
        self.assertIn("無効", verdict["verdict"])

    def test_ineffective_when_unsupported_significantly_increases(self):
        pairs = [_pair(3, 0.1, 2) for _ in range(10)]
        verdict = compute_verdict(pairs, generator="agy")
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertFalse(verdict["unsupported_not_increased"])
        self.assertFalse(verdict["effective"])
        self.assertIn("裏付けの無い文の数が有意に増えている", verdict["verdict"])

    def test_none_diffs_are_excluded_from_bootstrap_input(self):
        pairs = [_pair(3, 0.1, -1) for _ in range(5)] + [_pair(None, None, None)]
        verdict = compute_verdict(pairs, generator="agy")
        self.assertEqual(verdict["count_diff_bootstrap_ci"]["n"], 5)

    def test_empty_pairs_not_effective(self):
        verdict = compute_verdict([], generator="agy")
        self.assertFalse(verdict["effective"])
        self.assertIsNone(verdict["count_diff_bootstrap_ci"]["lower"])

    def test_verdict_text_mentions_generator_name(self):
        verdict = compute_verdict([_pair(3, 0.1, -1) for _ in range(10)], generator="agy")
        self.assertIn("agy", verdict["verdict"])


class AgyCallBudgetTest(unittest.TestCase):
    def test_allows_until_limit_reached(self):
        budget = AgyCallBudget(3)
        self.assertTrue(budget.allow())
        budget.record(1)
        self.assertTrue(budget.allow())
        budget.record(2)
        self.assertFalse(budget.allow())
        self.assertTrue(budget.stopped)

    def test_record_can_exceed_limit_in_one_step(self):
        budget = AgyCallBudget(2)
        budget.record(5)
        self.assertEqual(budget.used, 5)
        self.assertTrue(budget.stopped)
        self.assertFalse(budget.allow())

    def test_starts_allowed_when_limit_positive(self):
        budget = AgyCallBudget(1)
        self.assertTrue(budget.allow())
        self.assertFalse(budget.stopped)


if __name__ == "__main__":
    unittest.main()
