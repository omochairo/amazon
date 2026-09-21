"""scripts/experimental/multistage_brief/run_c1_self_critique.py の pure function 単体テスト
(#4841 ③ 自己批判パス単独評価)。

run() は対象外 (test_run_m2.py と同じ方針)。compute_verdict (M2 の判定基準を
そのまま流用したブートストラップ判定) と summarize_gain を検証し、process_asin は
IO を全部差し替えて「C'=A のとき評価し直さない」ことだけを確かめる。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from scripts.experimental.multistage_brief import run_c1_self_critique
from scripts.experimental.multistage_brief.run_c1_self_critique import compute_verdict, summarize_gain


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


def _gain(unique_supported, sentences, unsupported, unresolved=0):
    return {
        "unique_and_supported_count": unique_supported, "sentence_count": sentences,
        "unsupported_count": unsupported, "unresolved_count": unresolved,
        "per_sentence": [], "entailment_call_meta": {"total_duration_s": 1.0},
    }


class SummarizeGainTest(unittest.TestCase):
    def test_keeps_unresolved_count(self):
        row = summarize_gain(_gain(3, 10, 1, unresolved=2))
        self.assertEqual(row["unresolved_count"], 2)
        self.assertEqual(row["ratio"], 0.3)

    def test_ratio_none_when_no_sentences(self):
        self.assertIsNone(summarize_gain(_gain(0, 0, 0))["ratio"])


class ProcessAsinReuseTest(unittest.TestCase):
    """C'=A (指摘ゼロ / 書き直しが空) なら C' の評価で gemma を呼び直さない。"""

    def _run(self, flagged, rewritten_narrative):
        narrative_a = {"lead": "Aの本文。", "how_to_choose": "選び方。"}
        m = run_c1_self_critique
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.object(m.raw_material, "load_raw_material", return_value={}),                 mock.patch.object(m.raw_material, "build_material_text", return_value="素材"),                 mock.patch.object(m.raw_material, "allowed_competitor_asins", return_value=set()),                 mock.patch.object(m.corpus, "sample_category_articles", return_value=[]),                 mock.patch.object(m.corpus, "sample_other_category_articles", return_value=[]),                 mock.patch.object(m.corpus, "per_key_max_sim", return_value={}),                 mock.patch.object(m.sentence_metrics, "build_sentence_pool", return_value=[]),                 mock.patch.object(m.narrative_stage, "generate_narrative_baseline",
                                  return_value={"narrative": narrative_a, "call_meta": {}}),                 mock.patch.object(m.critique_stage, "critique_paragraphs",
                                  return_value={"flagged": flagged, "call_meta": {}}),                 mock.patch.object(m.critique_stage, "rewrite_flagged",
                                  return_value={"narrative": rewritten_narrative(narrative_a),
                                                "rewritten_keys": [], "call_meta": None}),                 mock.patch.object(m.guardrails, "check_asin_containment", return_value={"ok": True}),                 mock.patch.object(m.sentence_metrics, "compute_information_gain",
                                  return_value=_gain(3, 10, 1)) as gain_mock:
            result = m.process_asin(
                {"asin": "B000000000", "category": "cat"}, ollama_url="x", ruri_url="y", model="m",
                num_ctx=1, session=None, run_dir=pathlib.Path(tmp),
            )
        return result, gain_mock.call_count

    def test_no_flags_reuses_a_evaluation(self):
        result, calls = self._run([], dict)
        self.assertEqual(calls, len(m_seeds()))
        self.assertTrue(all(r["c_identical_to_a"] for r in result["seed_runs"]))
        self.assertEqual(result["diff_count"], 0)
        self.assertEqual(result["diff_unresolved_count"], 0)

    def test_rewritten_narrative_is_evaluated(self):
        result, calls = self._run(
            [{"key": "lead", "reason": "定型句"}], lambda n: {**n, "lead": "書き直した本文。"},
        )
        self.assertEqual(calls, 2 * len(m_seeds()))
        self.assertFalse(any(r["c_identical_to_a"] for r in result["seed_runs"]))


def m_seeds():
    return run_c1_self_critique.noise_floor.DEFAULT_SEEDS


if __name__ == "__main__":
    unittest.main()
