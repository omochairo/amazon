"""scripts/experimental/search_query_trial/budget.py の単体テスト (#4841 V2 前提①)。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from scripts.experimental.search_query_trial.budget import (
    check_tavily_budget,
    month_used,
    refresh_ledger_from_origin_main,
)


def _write_usage(tmp_path: pathlib.Path, month: str, calls: int) -> pathlib.Path:
    path = tmp_path / "_tavily_usage.json"
    path.write_text(json.dumps({"month": month, "calls": calls}), encoding="utf-8")
    return path


class MonthUsedTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_returns_calls_for_matching_month(self):
        path = _write_usage(self.tmp_path, "2026-09", 374)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.assertEqual(month_used(path, now), 374)

    def test_returns_zero_for_different_month(self):
        path = _write_usage(self.tmp_path, "2026-08", 900)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(month_used(path, now), 0)

    def test_returns_zero_when_file_missing(self):
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.assertEqual(month_used(self.tmp_path / "nonexistent.json", now), 0)


class CheckTavilyBudgetTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_feasible_when_projection_under_budget(self):
        path = _write_usage(self.tmp_path, "2026-09", 374)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        report = check_tavily_budget(path, now=now)
        # 374 + 15日 * 27 + 40 = 819 <= 900
        self.assertTrue(report["feasible"])
        self.assertEqual(report["remaining_days_in_month"], 15)
        self.assertAlmostEqual(report["projected_total"], 819.0)

    def test_not_feasible_when_projection_exceeds_budget(self):
        path = _write_usage(self.tmp_path, "2026-09", 850)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        report = check_tavily_budget(path, now=now)
        self.assertFalse(report["feasible"])

    def test_remaining_days_includes_today(self):
        path = _write_usage(self.tmp_path, "2026-09", 0)
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        report = check_tavily_budget(path, now=now)
        self.assertEqual(report["remaining_days_in_month"], 1)

    def test_different_month_resets_used_to_zero(self):
        path = _write_usage(self.tmp_path, "2026-08", 900)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        report = check_tavily_budget(path, now=now)
        self.assertEqual(report["used_this_month"], 0)

    def test_does_not_leak_api_key_or_secret_material(self):
        path = _write_usage(self.tmp_path, "2026-09", 374)
        report = check_tavily_budget(path, now=datetime(2026, 9, 16, tzinfo=timezone.utc))
        dumped = json.dumps(report)
        self.assertNotIn("TAVILY_API_KEY", dumped)

    def test_usage_data_overrides_local_file(self):
        """owner修正6: git fetch で取得した最新の台帳内容を優先して使う。"""
        path = _write_usage(self.tmp_path, "2026-09", 374)  # ローカル (古い)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        report = check_tavily_budget(
            path, now=now, usage_data={"month": "2026-09", "calls": 400},
        )
        self.assertEqual(report["used_this_month"], 400)
        self.assertEqual(report["usage_source"], "origin_main")

    def test_local_file_used_when_usage_data_not_passed(self):
        path = _write_usage(self.tmp_path, "2026-09", 374)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        report = check_tavily_budget(path, now=now)
        self.assertEqual(report["used_this_month"], 374)
        self.assertEqual(report["usage_source"], "local_file")


class RefreshLedgerFromOriginMainTest(unittest.TestCase):
    def test_returns_parsed_json_from_git_show(self):
        payload = {"month": "2026-09", "calls": 401}

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(cmd, 0)
            self.assertEqual(cmd[:2], ["git", "show"])
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload))

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = refresh_ledger_from_origin_main()
        self.assertEqual(result, payload)

    def test_returns_none_when_fetch_fails(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["git", "fetch"]),
        ):
            result = refresh_ledger_from_origin_main()
        self.assertIsNone(result)

    def test_returns_none_when_show_output_not_json(self):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="not json")

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = refresh_ledger_from_origin_main()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
