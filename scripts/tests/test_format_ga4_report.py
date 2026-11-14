"""format_ga4_report の「特集 5 ページ・診断（月ごと）」の集計と表示 (navi-brain#166 F18)。"""
from __future__ import annotations

from scripts.format_ga4_report import render, summarize_feature_events


def test_summarize_feature_events_groups_and_normalizes_pages():
    rows = [
        {"yearMonth": "202611", "eventName": "diagnosis_start", "pagePath": "/diagnosis/index.html", "eventCount": 4},
        {"yearMonth": "202611", "eventName": "diagnosis_complete", "pagePath": "/diagnosis/", "eventCount": 2},
        {"yearMonth": "202611", "eventName": "diagnosis_start", "pagePath": "/other/", "eventCount": 99},
        {"yearMonth": "202610", "eventName": "affiliate_click", "pagePath": "/ranking/index.html", "eventCount": 3},
        {"yearMonth": "202610", "eventName": "affiliate_click", "pagePath": "/ranking", "eventCount": 2},
        {"yearMonth": "202610", "eventName": "affiliate_click", "pagePath": "/products/x/", "eventCount": 50},
    ]
    result = summarize_feature_events(rows)
    assert [month["yearMonth"] for month in result] == ["202610", "202611"]
    assert result[0]["affiliate"]["/ranking/"] == 5
    assert result[0]["affiliate_total"] == 5
    assert result[1]["diagnosis_start"] == 4
    assert result[1]["diagnosis_complete"] == 2


def test_render_feature_events_includes_tables_and_zero_start_rate():
    report = render({"feature_events": {"range": {"start": "2026-10-01", "end": "2026-11-14"}, "rows": [
        {"yearMonth": "202611", "eventName": "diagnosis_complete", "pagePath": "/diagnosis/", "eventCount": 1},
        {"yearMonth": "202611", "eventName": "affiliate_click", "pagePath": "/price/", "eventCount": 2},
    ]}}, 20, 10)
    assert "## 特集 5 ページ・診断（月ごと）" in report
    assert "対象期間: 2026-10-01 〜 2026-11-14" in report
    assert "| 2026-11 | 0 | 1 | — | 0 | 0 |" in report
    assert "| 2026-11 | 0 | 0 | 0 | 0 | 2 | 2 |" in report
    assert "今月は途中まで。" in report


def test_render_empty_feature_events_and_legacy_without_section():
    empty = render({"feature_events": {"range": {}, "rows": []}}, 20, 10)
    assert "## 特集 5 ページ・診断（月ごと）" in empty
    assert "（データなし）" in empty
    legacy = render({}, 20, 10)
    assert "## 特集 5 ページ・診断（月ごと）" not in legacy


def test_summarize_normalizes_query_and_counts_every_feature_page():
    rows = [
        {"yearMonth": "202611", "eventName": "affiliate_click", "pagePath": p, "eventCount": 1}
        for p in ["/diagnosis/?w=child", "/ranking", "/cospa/index.html", "/deals/", "/price"]
    ] + [
        {"yearMonth": "202611", "eventName": "diagnosis_pick_click", "pagePath": "/diagnosis", "eventCount": 3},
        {"yearMonth": "202611", "eventName": "diagnosis_share", "pagePath": "/diagnosis/index.html", "eventCount": 2},
        {"yearMonth": "202611", "eventName": "affiliate_click", "pagePath": "/price/b0xxxx/", "eventCount": 9},
    ]
    month = summarize_feature_events(rows)[0]
    assert month["affiliate"] == {"/diagnosis/": 1, "/ranking/": 1, "/cospa/": 1, "/deals/": 1, "/price/": 1}
    assert month["affiliate_total"] == 5, "特集ページの配下 (/price/<asin>/) は数えない"
    assert month["diagnosis_pick_click"] == 3
    assert month["diagnosis_share"] == 2


def test_render_completion_rate_and_broken_feature_payload():
    report = render({"feature_events": {"range": {"start": "2026-10-01", "end": "2026-11-12"}, "rows": [
        {"yearMonth": "202610", "eventName": "diagnosis_start", "pagePath": "/diagnosis/", "eventCount": 8},
        {"yearMonth": "202610", "eventName": "diagnosis_complete", "pagePath": "/diagnosis/", "eventCount": 6},
    ]}}, 20, 10)
    assert "| 2026-10 | 8 | 6 | 75.0% | 0 | 0 |" in report
    broken = render({"feature_events": {"range": None, "rows": None}}, 20, 10)
    assert "（データなし）" in broken
    no_month = render({"feature_events": {"range": {}, "rows": [{"yearMonth": "", "eventName": "diagnosis_start", "pagePath": "/diagnosis/", "eventCount": 1}]}}, 20, 10)
    assert "（データなし）" in no_month
