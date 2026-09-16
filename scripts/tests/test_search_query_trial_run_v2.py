"""scripts/experimental/search_query_trial/run_v2.py の単体テスト (#4841 V2)。

Tavily/gemma への実呼び出しはせず、各ステップをモックして配線を確認する。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.experimental.search_query_trial import run_v2 as R


class _FakeDeadHosts:
    def summary(self):
        return {"hosts": [], "skipped_requests": 0}


class RunOneGroupTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self._tmpdir.name) / "run"
        self.addCleanup(self._tmpdir.cleanup)
        self.session = requests.Session()

    def test_q0_does_not_call_tavily(self):
        with mock.patch.object(R, "resolve_product_identity", return_value=("title", "商品", "ブランド")), \
             mock.patch.object(R.gather, "q0_urls", return_value=["https://a"]), \
             mock.patch.object(R.gather, "fetch_bodies", return_value=([
                 {"text": "本文", "source_type": "blog", "source_url": "https://a"},
             ], [{"url": "https://a", "status": "ok"}])), \
             mock.patch.object(R.mining, "extract_snippets_checked",
                                return_value=([{"aspect": "体験談", "source_url": "https://a"}], {"status": "ok"})), \
             mock.patch.object(R.query_groups, "search_for_group") as fake_search:
            stats, raw = R.run_one_group(
                "Q0", "B0000000AA", None, base=pathlib.Path("data/raw/per_asin"),
                session=self.session, ollama_url="http://x", model="m", num_ctx=8192,
                run_dir=self.run_dir,
            )
        fake_search.assert_not_called()
        self.assertEqual(stats["urls_tried"], 1)
        self.assertEqual(stats["snippet_count"], 1)
        self.assertTrue((self.run_dir / "B0000000AA_Q0.json").exists())

    def test_q1_without_api_key_raises(self):
        with mock.patch.object(R, "resolve_product_identity", return_value=("t", "p", "b")):
            with self.assertRaises(RuntimeError):
                R.run_one_group(
                    "Q1", "B0000000AA", None, base=pathlib.Path("data/raw/per_asin"),
                    session=self.session, ollama_url="http://x", model="m", num_ctx=8192,
                    run_dir=None,
                )

    def test_q1_calls_tavily_search_and_records_new_query(self):
        with mock.patch.object(R, "resolve_product_identity", return_value=("t", "商品", "ブランド")), \
             mock.patch.object(R.query_groups, "search_for_group", return_value={
                 "asin": "B0000000AA", "group": "Q1", "query": "商品 使ってみた 感想",
                 "sources": [{"url": "https://a"}],
             }), \
             mock.patch.object(R.gather, "fetch_bodies", return_value=([], [{"url": "https://a", "status": "fetch_failed"}])), \
             mock.patch.object(R.mining, "extract_snippets_checked", return_value=([], {"status": "empty_text"})):
            stats, raw = R.run_one_group(
                "Q1", "B0000000AA", "tvly-test", base=pathlib.Path("data/raw/per_asin"),
                session=self.session, ollama_url="http://x", model="m", num_ctx=8192, run_dir=None,
            )
        self.assertEqual(raw["query"]["new_queries"], 1)
        self.assertEqual(raw["query"]["query"], "商品 使ってみた 感想")


class RunTest(unittest.TestCase):
    def test_runs_all_groups_for_all_asins_and_builds_report(self):
        asins = ["B0000000AA", "B0000000BB"]

        def fake_run_one_group(group, asin, api_key, **kwargs):
            stats = {
                "asin": asin, "group": group, "urls_tried": 1, "urls_fetch_failed": 0,
                "urls_fetch_success": 1, "snippet_count": 1 if group != "Q0" else 0,
                "snippet_per_success_url": 1.0 if group != "Q0" else 0.0,
                "aspect_breakdown": {}, "host_category_breakdown": {},
                "urls_checked_for_keyword": 0, "urls_with_product_or_brand_keyword": 0,
            }
            raw = {"asin": asin, "query": {"group": group, "new_queries": 1 if group != "Q0" else 0}}
            return stats, raw

        with mock.patch.object(R, "make_session", return_value=(self.__dict__.setdefault(
                "session", type("S", (), {})()), _FakeDeadHosts())), \
             mock.patch.object(R, "run_one_group", side_effect=fake_run_one_group):
            report = R.run(
                asins, api_key="tvly-test", ollama_url="http://x", model="m",
                num_ctx=8192, run_dir=None, sleeper=lambda s: None,
            )
        self.assertEqual(set(report["per_asin_group_stats"].keys()), set(asins))
        for asin in asins:
            self.assertEqual(set(report["per_asin_group_stats"][asin].keys()), {"Q0", "Q1", "Q2"})
        # Q1 + Q2 が新規query (ASINあたり2回) x 2 ASIN = 4
        self.assertEqual(report["new_query_count"], 4)
        self.assertIn("decision", report)


class MainCliTest(unittest.TestCase):
    def test_aborts_when_budget_infeasible(self):
        with mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": False, "used_this_month": 900}), \
             mock.patch.object(R.asin_selection, "select_v2_asins") as fake_select, \
             mock.patch("sys.argv", ["run_v2.py"]):
            code = R.main()
        self.assertEqual(code, 1)
        fake_select.assert_not_called()

    def test_dry_run_does_not_require_api_key(self):
        with mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": True}), \
             mock.patch.object(R.asin_selection, "select_v2_asins", return_value={
                 "candidate_count": 1, "selected": [{"asin": "B0000000AA", "category": "STEM"}],
                 "categories_covered": ["STEM"], "meets_min_categories": False, "seed": 1,
             }), \
             mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch("sys.argv", ["run_v2.py", "--dry-run"]):
            code = R.main()
        self.assertEqual(code, 0)

    def test_missing_api_key_aborts_without_dry_run(self):
        with mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": True}), \
             mock.patch.object(R.asin_selection, "select_v2_asins", return_value={
                 "candidate_count": 1, "selected": [{"asin": "B0000000AA", "category": "STEM"}],
                 "categories_covered": ["STEM"], "meets_min_categories": False, "seed": 1,
             }), \
             mock.patch.dict("os.environ", {"TAVILY_API_KEY": ""}, clear=False), \
             mock.patch("sys.argv", ["run_v2.py"]):
            code = R.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
