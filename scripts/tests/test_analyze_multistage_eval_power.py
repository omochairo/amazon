"""#4841 T2 (scripts/analyze_multistage_eval_power.py) の単体テスト。ネットワーク不要。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.analyze_multistage_eval_power import (
    _iso_week_label_to_monday,
    check_answerability_coverage_candidate,
    check_index_census_candidate,
    check_time_to_first_impression_candidate,
    extract_asin_from_page,
    mde_poisson_mean,
    mde_two_proportion,
    page_level_distribution,
)


class ExtractAsinTest(unittest.TestCase):
    def test_extracts_uppercase_asin(self):
        self.assertEqual(extract_asin_from_page("https://navi.omcha.jp/products/b0abcdefgh/"), "B0ABCDEFGH")

    def test_returns_none_for_non_product_page(self):
        self.assertIsNone(extract_asin_from_page("https://navi.omcha.jp/tags/knit/"))

    def test_returns_none_for_malformed_asin(self):
        self.assertIsNone(extract_asin_from_page("https://navi.omcha.jp/products/short/"))


class IsoWeekLabelTest(unittest.TestCase):
    def test_monday_of_week(self):
        # 2026-W23 の月曜日を手計算で確認 (date.fromisocalendar と同じ結果になるはず)
        from datetime import date
        expected = date.fromisocalendar(2026, 23, 1).isoformat()
        self.assertEqual(_iso_week_label_to_monday("2026-W23"), expected)


class MdeTwoProportionTest(unittest.TestCase):
    def test_larger_n_detects_smaller_effect(self):
        small_n = mde_two_proportion(0.02, 100)
        large_n = mde_two_proportion(0.02, 10000)
        self.assertIsNotNone(large_n)
        if small_n is not None:
            self.assertLess(large_n, small_n)

    def test_tiny_n_returns_none(self):
        self.assertIsNone(mde_two_proportion(0.02, 1))

    def test_zero_n_returns_none(self):
        self.assertIsNone(mde_two_proportion(0.02, 0))


class MdePoissonMeanTest(unittest.TestCase):
    def test_larger_n_detects_smaller_effect(self):
        small_n = mde_poisson_mean(1.5, 40)
        large_n = mde_poisson_mean(1.5, 800)
        self.assertLess(large_n, small_n)

    def test_zero_mean_returns_none(self):
        self.assertIsNone(mde_poisson_mean(0, 100))

    def test_known_closed_form(self):
        # e = (z_a+z_b) * sqrt(2/(mean*n)); mean=2, n=100 で手計算と一致することを確認
        import math
        z_sum = 1.9599639845400545 + 0.8416212335729143
        expected = z_sum * math.sqrt(2 / (2 * 100))
        self.assertAlmostEqual(mde_poisson_mean(2, 100), round(expected, 4), places=4)


class PageLevelDistributionTest(unittest.TestCase):
    def test_computes_union_and_lower_bound(self):
        weeks = [
            {
                "_week_label": "2026-W01",
                "totals": {"impressions_sitewide": 100, "clicks_sitewide": 2, "truncated_pages": True},
                "by_page": [
                    {"page": "https://x/products/b0aaaaaaaa/", "impressions": 10, "clicks": 1},
                    {"page": "https://x/products/b0bbbbbbbb/", "impressions": 0, "clicks": 0},
                ],
            },
            {
                "_week_label": "2026-W02",
                "totals": {"impressions_sitewide": 120, "clicks_sitewide": 3, "truncated_pages": True},
                "by_page": [
                    {"page": "https://x/products/b0aaaaaaaa/", "impressions": 5, "clicks": 0},
                    {"page": "https://x/products/b0cccccccc/", "impressions": 8, "clicks": 1},
                ],
            },
        ]
        result = page_level_distribution(weeks, total_article_pages=10)
        self.assertEqual(result["distinct_article_pages_ever_with_impressions_top100"], 2)  # A, C (B had 0)
        self.assertEqual(result["lower_bound_fraction_with_impressions"], 0.2)
        self.assertEqual(result["avg_impressions_per_article_per_week_sitewide"], 12.0)  # latest week (W02) / 10
        self.assertEqual(result["impressions_per_page_week_observed"]["n"], 3)  # 10, 5, 8 (0 excluded)

    def test_handles_missing_sitewide_totals_in_early_weeks(self):
        weeks = [
            {"_week_label": "2026-W01", "totals": {}, "by_page": []},
            {
                "_week_label": "2026-W02",
                "totals": {"impressions_sitewide": 100, "clicks_sitewide": 5},
                "by_page": [],
            },
        ]
        result = page_level_distribution(weeks, total_article_pages=10)
        self.assertEqual(result["weeks_with_sitewide_totals_field"], 1)
        self.assertEqual(result["avg_impressions_per_article_per_week_sitewide"], 10.0)


class AlternativeCandidateTest(unittest.TestCase):
    def test_index_census_candidate_missing_file(self):
        result = check_index_census_candidate(census_path="/nonexistent/path.json")
        self.assertFalse(result["measurable"])

    def test_index_census_candidate_reads_real_shape(self):
        with tempfile.TemporaryDirectory() as td:
            census_path = pathlib.Path(td) / "census.json"
            census_path.write_text(json.dumps({"totals": {"indexed": 80, "inspected": 100}}), encoding="utf-8")
            result = check_index_census_candidate(census_path=census_path, history_path="/nonexistent.jsonl")
            self.assertTrue(result["measurable"])
            self.assertEqual(result["baseline_indexed_rate"], 0.8)
            self.assertEqual(result["history_rate_trend"], [])

    def test_answerability_candidate_missing_file(self):
        result = check_answerability_coverage_candidate(path="/nonexistent/path.json")
        self.assertFalse(result["measurable"])

    def test_answerability_candidate_reports_small_sample_as_unmeasurable(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "audit.json"
            path.write_text(json.dumps({"pages": [{"page_coverage_avg": 0.5}, {"page_coverage_avg": 0.7}]}),
                             encoding="utf-8")
            result = check_answerability_coverage_candidate(path=path)
            self.assertFalse(result["measurable"])
            self.assertEqual(result["current_sample_size"], 2)

    def test_time_to_first_impression_reports_low_hit_rate(self):
        with tempfile.TemporaryDirectory() as td:
            articles_dir = pathlib.Path(td)
            (articles_dir / "2026-08-01-B0AAAAAAAA.json").write_text(
                json.dumps({"date": "2026-08-01T10:00:00Z", "product": {"asin": "B0AAAAAAAA"}}), encoding="utf-8",
            )
            (articles_dir / "2026-08-02-B0BBBBBBBB.json").write_text(
                json.dumps({"date": "2026-08-02T10:00:00Z", "product": {"asin": "B0BBBBBBBB"}}), encoding="utf-8",
            )
            weeks = [{
                "_week_label": "2026-W31",
                "by_page": [{"page": "https://x/products/b0aaaaaaaa/", "impressions": 5}],
            }]
            result = check_time_to_first_impression_candidate(weeks, articles_dir, recent_cutoff="2026-07-01")
            self.assertFalse(result["measurable"])
            self.assertEqual(result["recent_articles_in_window"], 2)
            self.assertEqual(result["recent_articles_ever_observed_in_top100"], 1)
            self.assertEqual(result["observable_hit_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
