"""_api_health / check_api_health の単体テスト (#8272)。"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _api_health  # noqa: E402
import check_api_health  # noqa: E402
import fetch_cross_search  # noqa: E402


def _verdicts(by_api):
    return {r["api"]: r["reason"] for r in check_api_health.evaluate(by_api)}


class TestRecord(unittest.TestCase):
    def test_noop_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_api_health.ENV_VAR, None)
            _api_health.record("x", 200)  # 例外にならないこと

    def test_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "h.jsonl")
            with mock.patch.dict(os.environ, {_api_health.ENV_VAR: log}):
                _api_health.record("rakuten_ranking", 200)
                _api_health.record("rakuten_ranking", None)
            self.assertEqual(check_api_health.load(log), {"rakuten_ranking": [200, None]})

    def test_unwritable_path_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "missing-dir", "h.jsonl")
            with mock.patch.dict(os.environ, {_api_health.ENV_VAR: log}):
                _api_health.record("x", 200)


class TestEvaluate(unittest.TestCase):
    def test_single_deprecated_call_is_unhealthy(self):
        # fetch_rakuten の Search は 1 run 1 回。廃止時の 400 はこれ 1 回で赤にする
        self.assertTrue(_verdicts({"rakuten_ichiba_search": [400]})["rakuten_ichiba_search"])

    def test_normal_background_400_is_healthy(self):
        # 廃止前の実 run では Rakuten 呼び出し約 340 回中 400 が 12 回
        statuses = [200] * 328 + [400] * 12
        self.assertEqual(_verdicts({"a": statuses})["a"], "")

    def test_majority_4xx_is_unhealthy(self):
        self.assertTrue(_verdicts({"a": [200] * 4 + [403] * 6})["a"])

    def test_429_is_not_persistent(self):
        self.assertEqual(_verdicts({"a": [200] + [429] * 9})["a"], "")

    def test_total_outage_is_unhealthy(self):
        self.assertTrue(_verdicts({"a": [None, 503, 429]})["a"])

    def test_two_transient_failures_are_tolerated(self):
        self.assertEqual(_verdicts({"a": [None, 503]})["a"], "")


class TestMain(unittest.TestCase):
    def _run(self, rows, summary_path=None):
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "h.jsonl"
            log.write_text("".join(json.dumps(r) + "\n" for r in rows) + "broken\n",
                           encoding="utf-8")
            env = {"GITHUB_STEP_SUMMARY": summary_path} if summary_path else {}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "argv", ["check_api_health.py", "--log", str(log)]):
                return check_api_health.main()

    def test_exit_1_when_unhealthy(self):
        self.assertEqual(self._run([{"api": "a", "status": 400}]), 1)

    def test_exit_0_when_healthy_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as d:
            summary = os.path.join(d, "summary.md")
            self.assertEqual(self._run([{"api": "a", "status": 200}], summary), 0)
            self.assertIn("| a | 1 | 1 | 0 | - | OK |",
                          pathlib.Path(summary).read_text(encoding="utf-8"))

    def test_missing_log_is_ok(self):
        with mock.patch.object(sys, "argv", ["check_api_health.py", "--log", "/nonexistent/x"]):
            self.assertEqual(check_api_health.main(), 0)


class TestCrossSearchGetRecords(unittest.TestCase):
    def test_records_status_and_network_error(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "h.jsonl")
            resp = mock.Mock(status_code=400)
            err = fetch_cross_search.requests.exceptions.ConnectionError("boom")
            with mock.patch.dict(os.environ, {_api_health.ENV_VAR: log}), \
                    mock.patch.object(fetch_cross_search.requests, "get",
                                      side_effect=[resp, err]):
                self.assertIs(fetch_cross_search._get("api", "http://x"), resp)
                with self.assertRaises(fetch_cross_search.requests.exceptions.ConnectionError):
                    fetch_cross_search._get("api", "http://x")
            self.assertEqual(check_api_health.load(log), {"api": [400, None]})


if __name__ == "__main__":
    unittest.main()
