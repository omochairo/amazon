"""scripts/fetch_ga4_article_feedback.py unit tests (issue #2051).

_month_range / _resolve_range は pure function (GA4 API 呼び出しに依存しない)
なので google-analytics-data をモックせず直接テストする。
"""
from __future__ import annotations

from scripts.fetch_ga4_article_feedback import _month_range, _resolve_range


def test_month_range_regular_month():
    assert _month_range("2026-06") == ("2026-06-01", "2026-06-30")


def test_month_range_december_rolls_into_next_year():
    assert _month_range("2026-12") == ("2026-12-01", "2026-12-31")


def test_month_range_february_non_leap_year():
    assert _month_range("2026-02") == ("2026-02-01", "2026-02-28")


def test_month_range_february_leap_year():
    assert _month_range("2024-02") == ("2024-02-01", "2024-02-29")


def test_resolve_range_prefers_month_over_days():
    start, end = _resolve_range(days=7, end_date="2026-06-15", month="2026-05")
    assert (start, end) == ("2026-05-01", "2026-05-31")


def test_resolve_range_uses_days_and_end_date_when_no_month():
    start, end = _resolve_range(days=30, end_date="2026-06-30", month=None)
    assert end == "2026-06-30"
    assert start == "2026-05-31"
