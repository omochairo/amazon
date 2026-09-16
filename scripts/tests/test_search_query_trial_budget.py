"""scripts/experimental/search_query_trial/budget.py の単体テスト (#4841 V2 前提①)。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

from scripts.experimental.search_query_trial.budget import check_tavily_budget, month_used


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


if __name__ == "__main__":
    unittest.main()
