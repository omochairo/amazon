"""scripts/append_analytics_history.py unit tests (B-0, epic #1357)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.append_analytics_history import (
    GA4_PAGES_FILE,
    GA4_TOTALS_FILE,
    GSC_BY_PAGE_FILE,
    GSC_BY_QUERY_FILE,
    GSC_LANE_WP,
    GSC_TOTALS_FILE,
    GSC_WP_BY_PAGE_FILE,
    GSC_WP_BY_QUERY_FILE,
    GSC_WP_TOTALS_FILE,
    SEEN_DATES_FILENAME,
    append_jsonl,
    load_seen_dates,
    process_ga4,
    process_gsc,
    save_seen_dates,
)


@pytest.fixture()
def gsc_fixture():
    return {
        "range": {"start": "2026-05-30", "end": "2026-05-30", "days": 1, "delay_days": 3},
        "totals": {"queries": 2, "pages": 1, "clicks_sum": 5, "impressions_sum": 150},
        "by_query": [
            {"query": "ロンビー", "clicks": 3, "impressions": 80, "ctr": 0.0375, "position": 4.0},
            {"query": "知育玩具", "clicks": 2, "impressions": 70, "ctr": 0.0286, "position": 6.5},
        ],
        "by_page": [
            {"page": "/products/B0X/", "clicks": 5, "impressions": 150, "ctr": 0.033, "position": 5.0},
        ],
        "by_combo": [],
    }


@pytest.fixture()
def ga4_fixture():
    return {
        "range": {"start": "2026-05-30", "end": "2026-05-30", "days": 1},
        "totals": {"rows_by_page": 2, "screenPageViews_sum": 20, "engagedSessions_sum": 8},
        "by_page": [
            {"hostName": "navi.omcha.jp", "pagePath": "/products/B0X/",
             "screenPageViews": 15, "engagedSessions": 6,
             "averageSessionDuration": 45.0, "bounceRate": 0.4,
             "engagementRate": 0.62},
            {"hostName": "navi.omcha.jp", "pagePath": "/posts/foo/",
             "screenPageViews": 5, "engagedSessions": 2,
             "averageSessionDuration": 30.0, "bounceRate": 0.6},
        ],
    }


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_append_jsonl_creates_file_and_writes_lines(tmp_path):
    out = tmp_path / "x.jsonl"
    n = append_jsonl(out, [{"a": 1}, {"a": 2}])
    assert n == 2
    rows = _read_jsonl(out)
    assert rows == [{"a": 1}, {"a": 2}]


def test_append_jsonl_appends_to_existing(tmp_path):
    out = tmp_path / "x.jsonl"
    append_jsonl(out, [{"a": 1}])
    append_jsonl(out, [{"a": 2}])
    rows = _read_jsonl(out)
    assert rows == [{"a": 1}, {"a": 2}]


def test_append_jsonl_no_rows_noop(tmp_path):
    out = tmp_path / "x.jsonl"
    n = append_jsonl(out, [])
    assert n == 0
    assert not out.exists()


def test_process_gsc_writes_three_files(tmp_path, gsc_fixture):
    seen = {}
    n = process_gsc(gsc_fixture, tmp_path, seen)
    assert n == 4  # 2 queries + 1 page + 1 totals
    assert seen["gsc"]["2026-05-30"] is True

    by_query = _read_jsonl(tmp_path / GSC_BY_QUERY_FILE)
    by_page = _read_jsonl(tmp_path / GSC_BY_PAGE_FILE)
    totals = _read_jsonl(tmp_path / GSC_TOTALS_FILE)

    assert len(by_query) == 2
    assert by_query[0]["date"] == "2026-05-30"
    assert by_query[0]["query"] in ("ロンビー", "知育玩具")
    assert len(by_page) == 1
    assert by_page[0]["page"] == "/products/B0X/"
    assert totals == [{
        "date": "2026-05-30",
        "clicks_sum": 5,
        "impressions_sum": 150,
        "queries_count": 2,
        "pages_count": 1,
        "clicks_sitewide": None,
        "impressions_sitewide": None,
        "ctr_sitewide": None,
        "position_sitewide": None,
        "truncated_pages": False,
        "truncated_queries": False,
    }]


def test_process_gsc_idempotent_same_date(tmp_path, gsc_fixture):
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen)
    # re-run with same data should be no-op
    n = process_gsc(gsc_fixture, tmp_path, seen)
    assert n == 0
    by_query = _read_jsonl(tmp_path / GSC_BY_QUERY_FILE)
    assert len(by_query) == 2  # まだ 2 行のまま


def test_process_gsc_different_dates_accumulate(tmp_path, gsc_fixture):
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen)
    # 翌日分
    gsc2 = json.loads(json.dumps(gsc_fixture))
    gsc2["range"]["end"] = "2026-05-31"
    process_gsc(gsc2, tmp_path, seen)
    by_query = _read_jsonl(tmp_path / GSC_BY_QUERY_FILE)
    dates = {row["date"] for row in by_query}
    assert dates == {"2026-05-30", "2026-05-31"}
    assert len(by_query) == 4


def test_process_gsc_no_range_end_skips(tmp_path, gsc_fixture):
    gsc_fixture["range"] = {}
    seen = {}
    n = process_gsc(gsc_fixture, tmp_path, seen)
    assert n == 0
    assert seen == {}


def test_process_ga4_writes_two_files(tmp_path, ga4_fixture):
    seen = {}
    n = process_ga4(ga4_fixture, tmp_path, seen)
    assert n == 3  # 2 pages + 1 totals
    assert seen["ga4"]["2026-05-30"] is True

    pages = _read_jsonl(tmp_path / GA4_PAGES_FILE)
    totals = _read_jsonl(tmp_path / GA4_TOTALS_FILE)
    assert len(pages) == 2
    assert pages[0]["hostName"] == "navi.omcha.jp"
    assert pages[0]["date"] == "2026-05-30"
    assert pages[0]["engagementRate"] == 0.62
    # 2 行目の fixture は engagementRate を持たない = 旧 artifact 相当。
    # 0.0 に潰すと A-4 (engagementRate < 0.30) の誤検出になるので None で残す
    assert pages[1]["engagementRate"] is None
    assert totals == [{
        "date": "2026-05-30",
        "screenPageViews_sum": 20,
        "engagedSessions_sum": 8,
        "rows_by_page": 2,
    }]


def test_process_ga4_idempotent(tmp_path, ga4_fixture):
    seen = {}
    process_ga4(ga4_fixture, tmp_path, seen)
    n = process_ga4(ga4_fixture, tmp_path, seen)
    assert n == 0


def test_save_and_load_seen_dates_roundtrip(tmp_path):
    seen_path = tmp_path / SEEN_DATES_FILENAME
    save_seen_dates(seen_path, {"gsc": {"2026-05-30": True}, "ga4": {}})
    loaded = load_seen_dates(seen_path)
    assert loaded["gsc"]["2026-05-30"] is True
    assert loaded["ga4"] == {}


def test_load_seen_dates_missing_returns_empty(tmp_path):
    assert load_seen_dates(tmp_path / "nope.json") == {}


def test_process_gsc_carries_sitewide_fields_navi(tmp_path, gsc_fixture):
    """#3988 B-1: totals.*_sitewide / truncated_* が totals_row にそのまま伝播する (navi レーン)。"""
    gsc_fixture["totals"].update({
        "clicks_sitewide": 42,
        "impressions_sitewide": 999,
        "ctr_sitewide": 0.042,
        "position_sitewide": 7.5,
        "truncated_pages": True,
        "truncated_queries": False,
    })
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen)
    totals = _read_jsonl(tmp_path / GSC_TOTALS_FILE)
    assert totals[0]["clicks_sitewide"] == 42
    assert totals[0]["impressions_sitewide"] == 999
    assert totals[0]["ctr_sitewide"] == 0.042
    assert totals[0]["position_sitewide"] == 7.5
    assert totals[0]["truncated_pages"] is True
    assert totals[0]["truncated_queries"] is False


def test_process_gsc_carries_sitewide_fields_wp(tmp_path, gsc_fixture):
    """同上、WP レーン。"""
    gsc_fixture["totals"].update({
        "clicks_sitewide": 100,
        "impressions_sitewide": 2000,
        "ctr_sitewide": 0.05,
        "position_sitewide": 3.2,
        "truncated_pages": True,
        "truncated_queries": True,
    })
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen, GSC_LANE_WP)
    totals = _read_jsonl(tmp_path / GSC_WP_TOTALS_FILE)
    assert totals[0]["clicks_sitewide"] == 100
    assert totals[0]["impressions_sitewide"] == 2000
    assert totals[0]["ctr_sitewide"] == 0.05
    assert totals[0]["position_sitewide"] == 3.2
    assert totals[0]["truncated_pages"] is True
    assert totals[0]["truncated_queries"] is True


def test_process_gsc_missing_sitewide_keys_default_none_and_false(tmp_path, gsc_fixture):
    """旧スキーマ (sitewide キー無し) の入力でもクラッシュせず None / False にデフォルトする。"""
    # gsc_fixture の totals には元々 *_sitewide / truncated_* キーが無い
    assert "clicks_sitewide" not in gsc_fixture["totals"]
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen)
    totals = _read_jsonl(tmp_path / GSC_TOTALS_FILE)
    assert totals[0]["clicks_sitewide"] is None
    assert totals[0]["impressions_sitewide"] is None
    assert totals[0]["ctr_sitewide"] is None
    assert totals[0]["position_sitewide"] is None
    assert totals[0]["truncated_pages"] is False
    assert totals[0]["truncated_queries"] is False


def test_process_gsc_handles_missing_optional_fields(tmp_path):
    """各 row が欠損 key を持っていてもデフォルトで埋まる。"""
    gsc = {
        "range": {"end": "2026-05-30"},
        "totals": {},
        "by_query": [{"query": "x"}],  # clicks/impressions/ctr/position 欠損
        "by_page": [{}],  # page も欠損
    }
    seen = {}
    process_gsc(gsc, tmp_path, seen)
    by_query = _read_jsonl(tmp_path / GSC_BY_QUERY_FILE)
    by_page = _read_jsonl(tmp_path / GSC_BY_PAGE_FILE)
    totals = _read_jsonl(tmp_path / GSC_TOTALS_FILE)
    assert by_query[0] == {
        "date": "2026-05-30", "query": "x",
        "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0,
    }
    assert by_page[0]["page"] == ""
    assert totals[0]["clicks_sum"] == 0


# ---------------------------------------------------------------------------
# WP (omcha.jp) レーン — navi レーンと独立していることの担保
# ---------------------------------------------------------------------------

def test_process_gsc_wp_lane_writes_separate_files(tmp_path, gsc_fixture):
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen, GSC_LANE_WP)

    assert _read_jsonl(tmp_path / GSC_WP_BY_QUERY_FILE)
    assert _read_jsonl(tmp_path / GSC_WP_BY_PAGE_FILE)
    assert _read_jsonl(tmp_path / GSC_WP_TOTALS_FILE)
    # navi 側のファイルは作られない
    assert not (tmp_path / GSC_BY_QUERY_FILE).exists()
    assert not (tmp_path / GSC_TOTALS_FILE).exists()
    assert seen["gsc_wp"]["2026-05-30"] is True
    assert "gsc" not in seen


def test_process_gsc_both_lanes_same_date_do_not_block_each_other(tmp_path, gsc_fixture):
    """同じ日付でも navi / WP はそれぞれ独立に append される (seen キーが別)。"""
    seen = {}
    n_navi = process_gsc(gsc_fixture, tmp_path, seen)
    n_wp = process_gsc(gsc_fixture, tmp_path, seen, GSC_LANE_WP)

    assert n_navi > 0 and n_wp > 0
    assert len(_read_jsonl(tmp_path / GSC_TOTALS_FILE)) == 1
    assert len(_read_jsonl(tmp_path / GSC_WP_TOTALS_FILE)) == 1
    assert seen["gsc"]["2026-05-30"] is True
    assert seen["gsc_wp"]["2026-05-30"] is True


def test_process_gsc_wp_lane_is_idempotent(tmp_path, gsc_fixture):
    seen = {}
    process_gsc(gsc_fixture, tmp_path, seen, GSC_LANE_WP)
    n = process_gsc(gsc_fixture, tmp_path, seen, GSC_LANE_WP)
    assert n == 0
    assert len(_read_jsonl(tmp_path / GSC_WP_TOTALS_FILE)) == 1
