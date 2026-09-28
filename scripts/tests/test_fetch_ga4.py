"""scripts/fetch_ga4.py unit tests (#2710 navi truncation fix).

_merge_unique_rows / _build_entrances_by_key は pure function (GA4 API 呼び出しに
依存しない) なので、google-analytics-data クライアントをモックせず直接テストする。
"""
from __future__ import annotations

from datetime import date

from scripts.fetch_ga4 import _build_entrances_by_key, _merge_unique_rows, compute_range


def test_merge_unique_rows_appends_new_keys_only():
    base = [
        {"hostName": "omcha.jp", "pagePath": "/a/", "screenPageViews": 100},
        {"hostName": "omcha.jp", "pagePath": "/b/", "screenPageViews": 50},
    ]
    extra = [
        {"hostName": "navi.omcha.jp", "pagePath": "/products/x/", "screenPageViews": 2},
        {"hostName": "navi.omcha.jp", "pagePath": "/products/y/", "screenPageViews": 1},
    ]
    merged = _merge_unique_rows(base, extra, ("hostName", "pagePath"))
    assert len(merged) == 4
    assert {"hostName": "navi.omcha.jp", "pagePath": "/products/x/", "screenPageViews": 2} in merged


def test_merge_unique_rows_skips_keys_already_in_base():
    # navi ページが既に合算レポートの top-N に入っていた場合 (稀に homepage 等)、
    # navi 専用フィルタの重複行は追加しない。
    base = [
        {"hostName": "navi.omcha.jp", "pagePath": "/", "screenPageViews": 10},
    ]
    extra = [
        {"hostName": "navi.omcha.jp", "pagePath": "/", "screenPageViews": 10},
        {"hostName": "navi.omcha.jp", "pagePath": "/products/z/", "screenPageViews": 1},
    ]
    merged = _merge_unique_rows(base, extra, ("hostName", "pagePath"))
    assert len(merged) == 2
    assert merged[0] == {"hostName": "navi.omcha.jp", "pagePath": "/", "screenPageViews": 10}


def test_merge_unique_rows_empty_extra_is_noop():
    base = [{"hostName": "omcha.jp", "pagePath": "/a/", "screenPageViews": 100}]
    assert _merge_unique_rows(base, [], ("hostName", "pagePath")) == base


def test_build_entrances_by_key_sums_within_list():
    rows = [
        {"hostName": "omcha.jp", "landingPagePlusQueryString": "/a/?utm=1", "sessions": 3},
        {"hostName": "omcha.jp", "landingPagePlusQueryString": "/a/?utm=2", "sessions": 2},
    ]
    result = _build_entrances_by_key(rows)
    assert result[("omcha.jp", "/a/")] == 5


def test_build_entrances_by_key_merges_navi_rows_without_double_count():
    rows = [
        {"hostName": "omcha.jp", "landingPagePlusQueryString": "/a/", "sessions": 5},
        {"hostName": "navi.omcha.jp", "landingPagePlusQueryString": "/", "sessions": 10},
    ]
    navi_rows = [
        {"hostName": "navi.omcha.jp", "landingPagePlusQueryString": "/", "sessions": 10},
        {"hostName": "navi.omcha.jp", "landingPagePlusQueryString": "/products/z/", "sessions": 1},
    ]
    result = _build_entrances_by_key(rows, navi_rows)
    assert result[("navi.omcha.jp", "/")] == 10
    assert result[("navi.omcha.jp", "/products/z/")] == 1
    assert result[("omcha.jp", "/a/")] == 5


def test_build_entrances_by_key_none_navi_rows():
    rows = [{"hostName": "omcha.jp", "landingPagePlusQueryString": "/a/", "sessions": 1}]
    assert _build_entrances_by_key(rows, None) == {("omcha.jp", "/a/"): 1}


def test_compute_range_default_keeps_legacy_window():
    # 日次レーン (18) は --days 1 で range.end=今日 を履歴キーにしている。既定は変えない
    assert compute_range(1, today=date(2026, 9, 27)) == (date(2026, 9, 26), date(2026, 9, 27))
    assert compute_range(7, today=date(2026, 9, 27)) == (date(2026, 9, 20), date(2026, 9, 27))


def test_compute_range_weekly_processed_exact_seven_days():
    # 週次レーン (17): 当日・前日を外し、両端込みでちょうど 7 日
    start, end = compute_range(7, lag_days=2, inclusive_days=True, today=date(2026, 9, 27))
    assert (start, end) == (date(2026, 9, 19), date(2026, 9, 25))
    assert (end - start).days + 1 == 7


def test_compute_range_end_date_wins_over_lag():
    assert compute_range(7, end_date="2026-09-10", lag_days=2, inclusive_days=True,
                         today=date(2026, 9, 27)) == (date(2026, 9, 4), date(2026, 9, 10))
