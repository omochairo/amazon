"""scripts/experimental/search_query_trial/run_v2.py の単体テスト (#4841 V2)。

Tavily/gemma への実呼び出しはせず、各ステップをモックして配線を確認する。
"""
from __future__ import annotations

import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from scripts.experimental.search_query_trial import run_v2 as R
from scripts import fetch_third_party_sources as F

_NOW = datetime.datetime.now(datetime.timezone.utc)
_MONTH = _NOW.strftime("%Y-%m")


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
    def _fake_run_one_group(self, group, asin, api_key, **kwargs):
        stats = {
            "asin": asin, "group": group, "urls_tried": 1, "urls_fetch_failed": 0,
            "urls_fetch_success": 1, "snippet_count": 1 if group != "Q0" else 0,
            "snippet_per_success_url": 1.0 if group != "Q0" else 0.0,
            "aspect_breakdown": {}, "host_category_breakdown": {},
            "urls_checked_for_keyword": 0, "urls_with_product_or_brand_keyword": 0,
        }
        raw = {"asin": asin, "query": {"group": group, "new_queries": 1 if group != "Q0" else 0}}
        return stats, raw

    def test_runs_all_groups_for_all_asins_and_builds_report(self):
        asins = ["B0000000AA", "B0000000BB"]

        with mock.patch.object(R, "make_session", return_value=(self.__dict__.setdefault(
                "session", type("S", (), {})()), _FakeDeadHosts())), \
             mock.patch.object(R, "run_one_group", side_effect=self._fake_run_one_group), \
             mock.patch.object(R, "raw_call_count", return_value=0):
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
        self.assertEqual(report["failures"], [])

    def test_exception_in_one_pair_is_recorded_and_others_continue(self):
        """owner修正2: ASIN×群単位で例外を捕まえ、その組を失敗として記録して続行する。"""
        asins = ["B0000000AA", "B0000000BB"]

        def flaky_run_one_group(group, asin, api_key, **kwargs):
            if asin == "B0000000AA" and group == "Q1":
                raise RuntimeError("HTTP 429")
            return self._fake_run_one_group(group, asin, api_key, **kwargs)

        with mock.patch.object(R, "make_session", return_value=(self.__dict__.setdefault(
                "session", type("S", (), {})()), _FakeDeadHosts())), \
             mock.patch.object(R, "run_one_group", side_effect=flaky_run_one_group), \
             mock.patch.object(R, "raw_call_count", return_value=0):
            report = R.run(
                asins, api_key="tvly-test", ollama_url="http://x", model="m",
                num_ctx=8192, run_dir=None, sleeper=lambda s: None,
            )
        # 落ちた組は per_asin_group_stats に載らないが、run 全体は最後まで進む
        self.assertNotIn("Q1", report["per_asin_group_stats"]["B0000000AA"])
        self.assertEqual(set(report["per_asin_group_stats"]["B0000000AA"].keys()), {"Q0", "Q2"})
        self.assertEqual(set(report["per_asin_group_stats"]["B0000000BB"].keys()), {"Q0", "Q1", "Q2"})
        self.assertEqual(report["failures"], [
            {"asin": "B0000000AA", "group": "Q1", "error": "HTTP 429"},
        ])

    def test_exception_writes_error_marker_to_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "run"

            def flaky_run_one_group(group, asin, api_key, **kwargs):
                raise RuntimeError("boom")

            with mock.patch.object(R, "make_session", return_value=(type("S", (), {})(), _FakeDeadHosts())), \
                 mock.patch.object(R, "run_one_group", side_effect=flaky_run_one_group), \
                 mock.patch.object(R, "raw_call_count", return_value=0):
                report = R.run(
                    ["B0000000AA"], api_key="tvly-test", ollama_url="http://x", model="m",
                    num_ctx=8192, run_dir=run_dir, sleeper=lambda s: None,
                )
            marker = json.loads((run_dir / "B0000000AA_Q0.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "error")
            self.assertEqual(marker["error"], "boom")
            self.assertEqual(len(report["failures"]), 3)  # Q0/Q1/Q2 すべて失敗

    def test_resume_skips_pairs_with_existing_result_and_does_not_call_run_one_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp) / "run"
            run_dir.mkdir(parents=True)
            stats = {
                "asin": "B0000000AA", "group": "Q0", "urls_tried": 1, "urls_fetch_failed": 0,
                "urls_fetch_success": 1, "snippet_count": 0, "snippet_per_success_url": 0.0,
                "aspect_breakdown": {}, "host_category_breakdown": {},
                "urls_checked_for_keyword": 0, "urls_with_product_or_brand_keyword": 0,
            }
            (run_dir / "B0000000AA_Q0.json").write_text(json.dumps(
                {"status": "ok", "stats": stats, "query": {"group": "Q0", "new_queries": 0}},
            ), encoding="utf-8")
            (run_dir / "B0000000AA_Q1.json").write_text(json.dumps(
                {"status": "error", "error": "HTTP 429"},
            ), encoding="utf-8")

            called = []

            def fake_run_one_group(group, asin, api_key, **kwargs):
                called.append((asin, group))
                return self._fake_run_one_group(group, asin, api_key, **kwargs)

            with mock.patch.object(R, "make_session", return_value=(type("S", (), {})(), _FakeDeadHosts())), \
                 mock.patch.object(R, "run_one_group", side_effect=fake_run_one_group), \
                 mock.patch.object(R, "raw_call_count", return_value=0):
                report = R.run(
                    ["B0000000AA"], api_key="tvly-test", ollama_url="http://x", model="m",
                    num_ctx=8192, run_dir=run_dir, sleeper=lambda s: None, resume=True,
                )
        # Q0 (前回成功) と Q1 (前回失敗) は Tavily を叩き直さない。Q2 だけ新規に呼ぶ
        self.assertEqual(called, [("B0000000AA", "Q2")])
        self.assertEqual(report["per_asin_group_stats"]["B0000000AA"]["Q0"], stats)
        self.assertNotIn("Q1", report["per_asin_group_stats"]["B0000000AA"])
        self.assertEqual(report["failures"], [
            {"asin": "B0000000AA", "group": "Q1", "error": "HTTP 429"},
        ])

    def test_reports_tavily_calls_consumed_via_ledger_delta(self):
        """owner修正1: 台帳の差分 (今回の消費) を報告に出す。"""
        with mock.patch.object(R, "make_session", return_value=(type("S", (), {})(), _FakeDeadHosts())), \
             mock.patch.object(R, "run_one_group", side_effect=self._fake_run_one_group), \
             mock.patch.object(R, "raw_call_count", side_effect=[374, 414]):
            report = R.run(
                ["B0000000AA"], api_key="tvly-test", ollama_url="http://x", model="m",
                num_ctx=8192, run_dir=None, sleeper=lambda s: None,
            )
        self.assertEqual(report["tavily_usage_before"], 374)
        self.assertEqual(report["tavily_usage_after"], 414)
        self.assertEqual(report["tavily_calls_consumed"], 40)

    def test_consumed_uses_ledger_even_when_fetched_at_usage_is_larger(self):
        """バグ回帰: month_usage は fetched_at 由来の成功件数と実呼び出し回数の
        大きい方を返すため、成功件数側 (430) が台帳 (374→414) を上回ると
        差分が 0 に出ていた (#4841 V2 実測で発覚)。raw_call_count を使い、
        fetched_at 側が大きくても台帳の実差分 (40) が出ることを確認する。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            for i in range(430):
                d = base / f"B{i:09d}"
                d.mkdir(parents=True)
                (d / F.OUT_NAME).write_text(
                    json.dumps({"fetched_at": _NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")}),
                    encoding="utf-8",
                )
            (base / F.USAGE_NAME).write_text(
                json.dumps({"month": _MONTH, "calls": 374}), encoding="utf-8",
            )
            self.assertGreater(F.month_usage(base, now=_NOW), 374)  # fetched_at 側が上回る前提

            def fake_run_one_group(group, asin, api_key, **kwargs):
                (base / F.USAGE_NAME).write_text(
                    json.dumps({"month": _MONTH, "calls": 414}), encoding="utf-8",
                )
                return self._fake_run_one_group(group, asin, api_key, **kwargs)

            with mock.patch.object(R, "make_session",
                                    return_value=(type("S", (), {})(), _FakeDeadHosts())), \
                 mock.patch.object(R, "run_one_group", side_effect=fake_run_one_group):
                report = R.run(
                    ["B0000000AA"], base=base, api_key="tvly-test", ollama_url="http://x",
                    model="m", num_ctx=8192, run_dir=None, sleeper=lambda s: None,
                )
        self.assertEqual(report["tavily_usage_before"], 374)
        self.assertEqual(report["tavily_usage_after"], 414)
        self.assertEqual(report["tavily_calls_consumed"], 40)


class MainCliTest(unittest.TestCase):
    def test_aborts_when_budget_infeasible(self):
        with mock.patch.object(R.budget, "refresh_ledger_from_origin_main", return_value=None), \
             mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": False, "used_this_month": 900}), \
             mock.patch.object(R.asin_selection, "select_v2_asins") as fake_select, \
             mock.patch("sys.argv", ["run_v2.py"]):
            code = R.main()
        self.assertEqual(code, 1)
        fake_select.assert_not_called()

    def test_dry_run_does_not_require_api_key(self):
        with mock.patch.object(R.budget, "refresh_ledger_from_origin_main", return_value=None), \
             mock.patch.object(R.budget, "check_tavily_budget",
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
        with mock.patch.object(R.budget, "refresh_ledger_from_origin_main", return_value=None), \
             mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": True}), \
             mock.patch.object(R.asin_selection, "select_v2_asins", return_value={
                 "candidate_count": 1, "selected": [{"asin": "B0000000AA", "category": "STEM"}],
                 "categories_covered": ["STEM"], "meets_min_categories": False, "seed": 1,
             }), \
             mock.patch.dict("os.environ", {"TAVILY_API_KEY": ""}, clear=False), \
             mock.patch("sys.argv", ["run_v2.py"]):
            code = R.main()
        self.assertEqual(code, 1)

    def test_passes_refreshed_ledger_data_to_budget_check(self):
        """owner修正6: git fetch で取得した最新台帳をcheck_tavily_budgetへ渡す。"""
        with mock.patch.object(R.budget, "refresh_ledger_from_origin_main",
                                return_value={"month": "2026-09", "calls": 400}), \
             mock.patch.object(R.budget, "check_tavily_budget",
                                return_value={"feasible": False}) as fake_check, \
             mock.patch.object(R.asin_selection, "select_v2_asins") as fake_select, \
             mock.patch("sys.argv", ["run_v2.py"]):
            R.main()
        self.assertEqual(fake_check.call_args.kwargs["usage_data"], {"month": "2026-09", "calls": 400})
        fake_select.assert_not_called()

    def test_resume_flag_is_forwarded_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = str(pathlib.Path(tmp) / "v2_results.json")
            with mock.patch.object(R.budget, "refresh_ledger_from_origin_main", return_value=None), \
                 mock.patch.object(R.budget, "check_tavily_budget", return_value={"feasible": True}), \
                 mock.patch.object(R.asin_selection, "select_v2_asins", return_value={
                     "candidate_count": 1, "selected": [{"asin": "B0000000AA", "category": "STEM"}],
                     "categories_covered": ["STEM"], "meets_min_categories": False, "seed": 1,
                 }), \
                 mock.patch.object(R, "run", return_value={"decision": "no_go"}) as fake_run, \
                 mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-test"}, clear=False), \
                 mock.patch("sys.argv", ["run_v2.py", "--resume", "--out", out_path]):
                R.main()
        self.assertTrue(fake_run.call_args.kwargs["resume"])


if __name__ == "__main__":
    unittest.main()
