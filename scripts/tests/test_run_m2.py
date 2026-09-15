"""scripts/experimental/multistage_brief/run_m2.py の pure function 単体テスト (#4841 M2)。

IO を伴う run_phase1_fact_cards / process_asin_phase2 / run() は対象外
(test_run_m1.py と同じ方針)。compute_verdict (「M2 判定基準の差し替え」の
ブートストラップ判定) のみ検証する。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.run_m2 import compute_verdict


def _pair(diff_count, diff_ratio, diff_unsupported):
    return {"diff_count": diff_count, "diff_ratio": diff_ratio, "diff_unsupported_count": diff_unsupported}


class ComputeVerdictTest(unittest.TestCase):
    def test_effective_when_count_and_ratio_positive_and_unsupported_not_increased(self):
        # 全ペアで一貫して正の差、裏付け無しは一貫して0 (または負) → 信頼区間は0を含まない/下限>0にならない
        pairs = [_pair(3, 0.1, -1) for _ in range(10)]
        verdict = compute_verdict(pairs)
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertTrue(verdict["unsupported_not_increased"])
        self.assertTrue(verdict["d_effective"])
        self.assertIn("D有効", verdict["verdict"])

    def test_ineffective_when_diffs_straddle_zero(self):
        pairs = [_pair(d, r, 0) for d, r in zip([2, -2, 1, -1, 3, -3, 0, 2, -2, 1], [0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0, 0.1, -0.1, 0.05])]
        verdict = compute_verdict(pairs)
        self.assertFalse(verdict["d_effective"])
        self.assertIn("D無効", verdict["verdict"])

    def test_ineffective_when_unsupported_significantly_increases(self):
        pairs = [_pair(3, 0.1, 2) for _ in range(10)]
        verdict = compute_verdict(pairs)
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertFalse(verdict["unsupported_not_increased"])
        self.assertFalse(verdict["d_effective"])
        self.assertIn("裏付けの無い文の数が有意に増えている", verdict["verdict"])

    def test_none_diffs_are_excluded_from_bootstrap_input(self):
        pairs = [_pair(3, 0.1, -1) for _ in range(5)] + [_pair(None, None, None)]
        verdict = compute_verdict(pairs)
        self.assertEqual(verdict["count_diff_bootstrap_ci"]["n"], 5)

    def test_empty_pairs_not_effective(self):
        verdict = compute_verdict([])
        self.assertFalse(verdict["d_effective"])
        self.assertIsNone(verdict["count_diff_bootstrap_ci"]["lower"])


if __name__ == "__main__":
    unittest.main()
