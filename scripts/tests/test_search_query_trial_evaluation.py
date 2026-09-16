"""scripts/experimental/search_query_trial/evaluation.py の単体テスト (#4841 V2)。"""
from __future__ import annotations

import unittest

from scripts.experimental.search_query_trial.evaluation import (
    build_report,
    compute_group_stats,
    evaluate_group,
    has_product_or_brand_keyword,
    paired_diffs,
)


class HasProductOrBrandKeywordTest(unittest.TestCase):
    def test_matches_token_not_full_phrase(self):
        self.assertTrue(has_product_or_brand_keyword("BRIOのレールを買いました", "BRIO 木製レール", "BRIO"))

    def test_no_match_returns_false(self):
        self.assertFalse(has_product_or_brand_keyword("全然関係ない文章です", "BRIO 木製レール", "BRIO"))

    def test_no_keywords_returns_none(self):
        self.assertIsNone(has_product_or_brand_keyword("text", "", ""))


class ComputeGroupStatsTest(unittest.TestCase):
    def test_success_and_failed_urls_split_correctly(self):
        fetch_log = [
            {"url": "https://a", "status": "ok"},
            {"url": "https://b", "status": "fetch_failed"},
        ]
        snippets = [{"aspect": "不満", "source_url": "https://a"}]
        stats = compute_group_stats(
            asin="B0000000AA", group="Q1", tried_urls=["https://a", "https://b"],
            fetch_log=fetch_log, snippets=snippets, product_name="BRIO", brand="BRIO",
            url_texts={"https://a": "BRIOの本文"},
        )
        self.assertEqual(stats["urls_tried"], 2)
        self.assertEqual(stats["urls_fetch_success"], 1)
        self.assertEqual(stats["urls_fetch_failed"], 1)
        self.assertEqual(stats["snippet_count"], 1)
        self.assertEqual(stats["snippet_per_success_url"], 1.0)
        self.assertEqual(stats["aspect_breakdown"], {"不満": 1})
        self.assertTrue(stats["urls_with_product_or_brand_keyword"] >= 0)

    def test_zero_success_urls_yields_zero_rate_not_division_error(self):
        stats = compute_group_stats(
            asin="B0000000AA", group="Q0", tried_urls=["https://a"],
            fetch_log=[{"url": "https://a", "status": "fetch_failed"}],
            snippets=[], product_name="BRIO", brand="BRIO", url_texts={},
        )
        self.assertEqual(stats["snippet_per_success_url"], 0.0)

    def test_empty_body_counted_separately_from_fetch_failed(self):
        """owner修正4: empty_bodyは取得失敗ではないので、成功URLあたりの分母から落とさない。"""
        fetch_log = [
            {"url": "https://a", "status": "ok"},
            {"url": "https://b", "status": "empty_body"},
            {"url": "https://c", "status": "fetch_failed"},
        ]
        stats = compute_group_stats(
            asin="B0000000AA", group="Q1",
            tried_urls=["https://a", "https://b", "https://c"],
            fetch_log=fetch_log, snippets=[{"aspect": "不満", "source_url": "https://a"}],
            product_name="BRIO", brand="BRIO", url_texts={"https://a": "BRIOの本文"},
        )
        self.assertEqual(stats["urls_fetch_failed"], 1)
        self.assertEqual(stats["urls_empty_body"], 1)
        # 分母 (成功URL) は fetch_failed の1件だけを除いた2件 (ok + empty_body)
        self.assertEqual(stats["urls_fetch_success"], 2)
        self.assertEqual(stats["snippet_per_success_url"], 0.5)

    def test_extraction_issue_counts_and_denominator_excludes_them(self):
        """owner修正3: 切り詰め・失敗の件数を出し、除いた分母も併記する (主指標は変えない)。"""
        fetch_log = [
            {"url": "https://a", "status": "ok"},
            {"url": "https://b", "status": "ok"},
        ]
        extraction_meta = [
            {"source_url": "https://a", "status": "ok"},
            {"source_url": "https://b", "status": "truncated"},
        ]
        stats = compute_group_stats(
            asin="B0000000AA", group="Q1", tried_urls=["https://a", "https://b"],
            fetch_log=fetch_log, snippets=[{"aspect": "不満", "source_url": "https://a"}],
            product_name="BRIO", brand="BRIO",
            url_texts={"https://a": "BRIOの本文", "https://b": "BRIOの本文2"},
            extraction_meta=extraction_meta,
        )
        self.assertEqual(stats["extraction_ok"], 1)
        self.assertEqual(stats["extraction_truncated"], 1)
        self.assertEqual(stats["extraction_failed"], 0)
        self.assertEqual(stats["extraction_bad_json"], 0)
        # 主指標 (分母2件) は変えない
        self.assertEqual(stats["snippet_per_success_url"], 0.5)
        # 切り詰めのURLを除いた分母 (1件) だと同じ1snippetでも率が変わる
        self.assertEqual(stats["snippet_per_success_url_excl_extraction_issues"], 1.0)

    def test_host_category_breakdown_uses_v1_classification(self):
        stats = compute_group_stats(
            asin="B0000000AA", group="Q1", tried_urls=["https://note.com/1", "https://kakaku.com/2"],
            fetch_log=[{"url": u, "status": "ok"} for u in
                       ("https://note.com/1", "https://kakaku.com/2")],
            snippets=[], product_name="X", brand="X", url_texts={},
        )
        self.assertEqual(stats["host_category_breakdown"], {"blog": 1, "ec": 1})


class PairedDiffsAndEvaluateTest(unittest.TestCase):
    def _stats(self, asin, group, rate):
        return {
            "asin": asin, "group": group, "urls_fetch_success": 1,
            "snippet_per_success_url": rate,
        }

    def test_paired_diffs_only_uses_asins_with_both_groups(self):
        per_asin = {
            "B0000000AA": {"Q0": self._stats("B0000000AA", "Q0", 0.2), "Q1": self._stats("B0000000AA", "Q1", 0.5)},
            "B0000000BB": {"Q0": self._stats("B0000000BB", "Q0", 0.1)},  # Q1 欠落 -> 対象外
        }
        diffs = paired_diffs(per_asin, treatment="Q1")
        self.assertEqual(diffs, [0.3])

    def test_evaluate_group_valid_when_all_diffs_positive(self):
        per_asin = {
            f"B{i:09d}": {
                "Q0": self._stats(f"B{i:09d}", "Q0", 0.1),
                "Q1": self._stats(f"B{i:09d}", "Q1", 0.6),
            }
            for i in range(10)
        }
        result = evaluate_group(per_asin, treatment="Q1", n_resamples=200, seed=1)
        self.assertTrue(result["valid"])
        self.assertEqual(result["n_asin_pairs"], 10)

    def test_evaluate_group_invalid_when_no_consistent_effect(self):
        per_asin = {}
        for i in range(10):
            asin = f"B{i:09d}"
            rate_q1 = 0.6 if i % 2 == 0 else 0.05
            per_asin[asin] = {
                "Q0": self._stats(asin, "Q0", 0.3),
                "Q1": self._stats(asin, "Q1", rate_q1),
            }
        result = evaluate_group(per_asin, treatment="Q1", n_resamples=200, seed=1)
        self.assertFalse(result["valid"])

    def test_build_report_decision_reflects_either_group_valid(self):
        per_asin = {
            f"B{i:09d}": {
                "Q0": self._stats(f"B{i:09d}", "Q0", 0.1),
                "Q1": self._stats(f"B{i:09d}", "Q1", 0.6),
                "Q2": self._stats(f"B{i:09d}", "Q2", 0.1),
            }
            for i in range(10)
        }
        report = build_report(per_asin)
        self.assertEqual(report["decision"], "go")
        self.assertEqual(report["adopted_groups"], ["Q1"])
        self.assertTrue(report["q1_vs_q0"]["valid"])
        self.assertFalse(report["q2_vs_q0"]["valid"])

    def test_build_report_no_go_when_neither_group_valid(self):
        per_asin = {
            f"B{i:09d}": {
                "Q0": self._stats(f"B{i:09d}", "Q0", 0.1),
                "Q1": self._stats(f"B{i:09d}", "Q1", 0.1),
                "Q2": self._stats(f"B{i:09d}", "Q2", 0.1),
            }
            for i in range(10)
        }
        report = build_report(per_asin)
        self.assertEqual(report["decision"], "no_go")
        self.assertEqual(report["adopted_groups"], [])

    def test_evaluate_group_uses_bonferroni_confidence_when_passed(self):
        per_asin = {
            f"B{i:09d}": {
                "Q0": self._stats(f"B{i:09d}", "Q0", 0.1),
                "Q1": self._stats(f"B{i:09d}", "Q1", 0.6),
            }
            for i in range(10)
        }
        result = evaluate_group(per_asin, treatment="Q1", confidence=0.975)
        self.assertEqual(result["confidence"], 0.975)


if __name__ == "__main__":
    unittest.main()
