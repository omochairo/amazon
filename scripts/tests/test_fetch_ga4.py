"""scripts/fetch_ga4.py unit tests (#2710 navi truncation fix).

_merge_unique_rows / _build_entrances_by_key は pure function (GA4 API 呼び出しに
依存しない) なので、google-analytics-data クライアントをモックせず直接テストする。
"""
from __future__ import annotations

from scripts.fetch_ga4 import _build_entrances_by_key, _merge_unique_rows


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
