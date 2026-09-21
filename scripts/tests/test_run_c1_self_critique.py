"""scripts/experimental/multistage_brief/run_c1_self_critique.py の pure function 単体テスト
(#4841 ③ 自己批判パス単独評価)。

IO を伴う process_asin / run() は対象外 (test_run_m2.py と同じ方針)。compute_verdict
(M2 の判定基準をそのまま流用したブートストラップ判定) のみ検証する。
"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.run_c1_self_critique import compute_verdict


def _pair(diff_count, diff_ratio, diff_unsupported):
    return {"diff_count": diff_count, "diff_ratio": diff_ratio, "diff_unsupported_count": diff_unsupported}


class ComputeVerdictTest(unittest.TestCase):
    def test_effective_when_count_and_ratio_positive_and_unsupported_not_increased(self):
        pairs = [_pair(3, 0.1, -1) for _ in range(10)]
        verdict = compute_verdict(pairs)
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertTrue(verdict["unsupported_not_increased"])
        self.assertTrue(verdict["c_effective"])
        self.assertIn("C有効", verdict["verdict"])

    def test_ineffective_when_diffs_straddle_zero(self):
        pairs = [
            _pair(d, r, 0)
            for d, r in zip(
                [2, -2, 1, -1, 3, -3, 0, 2, -2, 1],
                [0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0, 0.1, -0.1, 0.05],
            )
        ]
        verdict = compute_verdict(pairs)
        self.assertFalse(verdict["c_effective"])
        self.assertIn("C無効", verdict["verdict"])

    def test_ineffective_when_unsupported_significantly_increases(self):
        pairs = [_pair(3, 0.1, 2) for _ in range(10)]
        verdict = compute_verdict(pairs)
        self.assertTrue(verdict["count_significant"])
        self.assertTrue(verdict["ratio_significant"])
        self.assertFalse(verdict["unsupported_not_increased"])
        self.assertFalse(verdict["c_effective"])
        self.assertIn("裏付けの無い文の数が有意に増えている", verdict["verdict"])

    def test_none_diffs_are_excluded_from_bootstrap_input(self):
        pairs = [_pair(3, 0.1, -1) for _ in range(5)] + [_pair(None, None, None)]
        verdict = compute_verdict(pairs)
        self.assertEqual(verdict["count_diff_bootstrap_ci"]["n"], 5)

    def test_empty_pairs_not_effective(self):
        verdict = compute_verdict([])
        self.assertFalse(verdict["c_effective"])
        self.assertIsNone(verdict["count_diff_bootstrap_ci"]["lower"])


if __name__ == "__main__":
    unittest.main()
