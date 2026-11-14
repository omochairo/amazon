"""scripts/fetch_ga4_affiliate_clicks.py の単体テスト (navi-brain#165 T6)。

GA4 API は呼ばない。client はリクエストを記録して用意した応答を返す偽物にし、
応答は google-analytics-data の本物の型で組み立てる。数字はすべて架空。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from google.analytics.data_v1beta.types import (
    DimensionValue, MetricValue, ResponseMetaData, Row, RunReportResponse, SamplingMetadata,
)

from scripts import fetch_ga4_affiliate_clicks as m


def _resp(rows, row_count=None, **meta):
    return RunReportResponse(
        rows=[Row(dimension_values=[DimensionValue(value=v) for v in dims],
                  metric_values=[MetricValue(value=str(n))]) for dims, n in rows],
        row_count=len(rows) if row_count is None else row_count,
        metadata=ResponseMetaData(**meta),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def run_report(self, req):
        self.requests.append(req)
        return self.responses.pop(0)


# ---------------------------------------------------------------- 日付の窓

def test_today_jst_crosses_utc_midnight():
    # UTC 11-13 15:30 は日本時間 11-14 00:30
    assert m.today_jst(datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)) == date(2026, 11, 14)
    assert m.today_jst(datetime(2026, 11, 13, 14, 59, tzinfo=timezone.utc)) == date(2026, 11, 13)


def test_default_window_drops_today_and_yesterday_and_is_inclusive():
    start, end = m.resolve_window(start=None, end=None, days=28, today=date(2026, 11, 15))
    assert end == date(2026, 11, 13)
    assert start == date(2026, 10, 17)
    assert (end - start).days + 1 == 28


def test_window_56_days_for_trend():
    start, end = m.resolve_window(start=None, end=None, days=56, today=date(2026, 11, 15))
    assert (end - start).days + 1 == 56


def test_explicit_start_end_win():
    start, end = m.resolve_window(start="2026-11-01", end="2026-11-07", days=28,
                                  today=date(2026, 11, 15))
    assert (start, end) == (date(2026, 11, 1), date(2026, 11, 7))


def test_end_only_counts_days_back():
    start, end = m.resolve_window(start=None, end="2026-11-07", days=7, today=date(2026, 11, 15))
    assert (start, end) == (date(2026, 11, 1), date(2026, 11, 7))


def test_reversed_window_is_rejected():
    with pytest.raises(ValueError):
        m.resolve_window(start="2026-11-08", end="2026-11-07", days=7, today=date(2026, 11, 15))
    with pytest.raises(ValueError):
        m.resolve_window(start=None, end=None, days=0, today=date(2026, 11, 15))


# ---------------------------------------------------------------- 行の整形

def test_parse_ga4_date():
    assert m.parse_ga4_date("20261105") == "2026-11-05"
    assert m.parse_ga4_date("(other)") == "(other)"


def test_rows_from_response_click_shape_keeps_not_set():
    res = _resp([(["20261105", "/products/b0bsh34yr8/", "buy-top", "amazon", "B0BSH34YR8"], 3),
                 (["20261105", "/ranking/", "(not set)", "rakuten", "(not set)"], 1)])
    rows = m.rows_from_response(res, m.CLICK_DIMS, m.CLICK_KEYS)
    assert rows[0] == {"date": "2026-11-05", "pagePath": "/products/b0bsh34yr8/",
                       "slot": "buy-top", "network": "amazon", "asin": "B0BSH34YR8",
                       "eventCount": 3}
    assert rows[1]["slot"] == "(not set)" and rows[1]["asin"] == "(not set)"


def test_rows_from_response_event_shape():
    res = _resp([(["20261105", "/", "page_view"], 12)])
    assert m.rows_from_response(res, m.EVENT_DIMS, m.EVENT_KEYS) == [
        {"date": "2026-11-05", "pagePath": "/", "eventName": "page_view", "eventCount": 12}]


def test_metadata_warnings():
    assert m.metadata_warnings(_resp([])) == []
    res = _resp([], subject_to_thresholding=True, data_loss_from_other_row=True,
                sampling_metadatas=[SamplingMetadata(samples_read_count=517, sampling_space_size=983)])
    warns = m.metadata_warnings(res)
    assert len(warns) == 3
    assert any("しきい値" in w for w in warns)
    assert any("サンプリング" in w for w in warns)
    assert any("(other)" in w for w in warns)
    # ログに出るので数値を入れない
    assert not any(("517" in w) or ("983" in w) for w in warns)


# ---------------------------------------------------------------- リクエスト

def _filter_fields(expr):
    """and_group の中の (field, 値) を取り出す。"""
    out = []
    for e in expr.and_group.expressions:
        f = e.filter
        if f.in_list_filter.values:
            out.append((f.field_name, tuple(f.in_list_filter.values)))
        else:
            out.append((f.field_name, f.string_filter.value,
                        f.string_filter.match_type.name))
    return out


def test_click_request_dims_filter_and_order():
    req = m.build_request("123", date(2026, 11, 1), date(2026, 11, 28), m.CLICK_DIMS,
                          m.click_filter(), offset=10000, limit=10000)
    assert req.property == "properties/123"
    assert [d.name for d in req.dimensions] == [
        "date", "pagePath", "customEvent:slot", "customEvent:network", "customEvent:asin"]
    assert [x.name for x in req.metrics] == ["eventCount"]
    assert (req.date_ranges[0].start_date, req.date_ranges[0].end_date) == ("2026-11-01", "2026-11-28")
    assert _filter_fields(req.dimension_filter) == [
        ("hostName", "navi.omcha.jp", "EXACT"), ("eventName", "affiliate_click", "EXACT")]
    assert [o.dimension.dimension_name for o in req.order_bys] == m.CLICK_DIMS
    assert (req.offset, req.limit) == (10000, 10000)


def test_event_request_filters_by_event_names():
    req = m.build_request("123", date(2026, 11, 1), date(2026, 11, 28), m.EVENT_DIMS,
                          m.event_filter())
    fields = _filter_fields(req.dimension_filter)
    assert fields[0] == ("hostName", "navi.omcha.jp", "EXACT")
    assert fields[1][0] == "eventName"
    names = set(fields[1][1])
    assert {"page_view", "affiliate_click", "m_sticky_tap", "sale_notice_click",
            "diagnosis_start", "diagnosis_complete", "article_feedback"} <= names


def test_event_names_exist_in_site_js():
    """EVENT_NAMES は実際に hugo/assets/js/ が送っている名前 (page_view は GA4 の自動送信)。"""
    import pathlib
    js = "".join(p.read_text(encoding="utf-8")
                 for p in (pathlib.Path(__file__).resolve().parents[2] / "hugo/assets/js").glob("*.js"))
    for name in m.EVENT_NAMES:
        if name == "page_view":
            continue
        assert f'"{name}"' in js or f"'{name}'" in js, name


# ---------------------------------------------------------------- ページ送り

def test_run_paged_follows_offset_until_row_count():
    page1 = _resp([(["20261105", f"/p{i}/", "page_view"], 1) for i in range(3)], row_count=7)
    page2 = _resp([(["20261105", f"/q{i}/", "page_view"], 1) for i in range(3)], row_count=7,
                  subject_to_thresholding=True)
    page3 = _resp([(["20261105", "/r/", "page_view"], 1)], row_count=7,
                  subject_to_thresholding=True)
    client = FakeClient([page1, page2, page3])
    rows, warns = m.run_paged(client, "1", date(2026, 11, 1), date(2026, 11, 7), m.EVENT_DIMS,
                              m.EVENT_KEYS, m.event_filter(), page_size=3)
    assert len(rows) == 7
    assert [r.offset for r in client.requests] == [0, 3, 6]
    assert all(r.limit == 3 for r in client.requests)
    assert len(warns) == 1  # 同じ警告は 1 回だけ


def test_run_paged_single_page_and_empty():
    client = FakeClient([_resp([])])
    rows, warns = m.run_paged(client, "1", date(2026, 11, 1), date(2026, 11, 7), m.EVENT_DIMS,
                              m.EVENT_KEYS, m.event_filter())
    assert rows == [] and warns == [] and len(client.requests) == 1


def test_run_paged_stops_when_page_is_empty_even_if_row_count_says_more():
    client = FakeClient([_resp([(["20261105", "/", "page_view"], 1)], row_count=5), _resp([], row_count=5)])
    rows, _ = m.run_paged(client, "1", date(2026, 11, 1), date(2026, 11, 7), m.EVENT_DIMS,
                          m.EVENT_KEYS, m.event_filter(), page_size=1)
    assert len(rows) == 1 and len(client.requests) == 2


def test_fetch_combines_both_queries():
    clicks = _resp([(["20261105", "/products/b0bsh34yr8/", "buy-top", "amazon", "B0BSH34YR8"], 2)])
    events = _resp([(["20261105", "/products/b0bsh34yr8/", "page_view"], 9)],
                   subject_to_thresholding=True)
    client = FakeClient([clicks, events])
    out = m.fetch(client, "1", date(2026, 11, 1), date(2026, 11, 7))
    assert out["meta"]["range"] == {"start": "2026-11-01", "end": "2026-11-07"}
    assert out["meta"]["host"] == "navi.omcha.jp"
    assert out["clicks"][0]["slot"] == "buy-top"
    assert out["events"][0]["eventName"] == "page_view"
    assert len(out["meta"]["warnings"]) == 1 and out["meta"]["warnings"][0].startswith("イベントの取得")


# ---------------------------------------------------------------- main

def test_main_writes_jsonl_and_meta(tmp_path, monkeypatch, capsys):
    out = {"meta": {"range": {"start": "2026-11-01", "end": "2026-11-07"}, "warnings": ["W"],
                    "host": "navi.omcha.jp", "fetched_at": "x"},
           "clicks": [{"date": "2026-11-05", "pagePath": "/", "slot": "sticky",
                       "network": "amazon", "asin": "", "eventCount": 1}],
           "events": [{"date": "2026-11-05", "pagePath": "/", "eventName": "page_view",
                       "eventCount": 4}]}
    monkeypatch.setattr(m, "fetch", lambda *a, **k: out)
    monkeypatch.setattr(m, "_load_credentials", lambda s: None)

    class _Client:
        def __init__(self, credentials=None):
            pass

    import google.analytics.data_v1beta as ga
    monkeypatch.setattr(ga, "BetaAnalyticsDataClient", _Client)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rows_path, meta_path = tmp_path / "rows.jsonl", tmp_path / "meta.json"
    rc = m.main(["--property-id", "1", "--sa-json", "{}", "--start", "2026-11-01",
                 "--end", "2026-11-07", "--out", str(rows_path), "--meta-out", str(meta_path)])
    assert rc == 0
    raw = rows_path.read_bytes()
    assert b"\r\n" not in raw
    lines = [json.loads(x) for x in raw.decode("utf-8").splitlines()]
    assert len(lines) == 2 and lines[1]["eventName"] == "page_view"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["warnings"] == ["W"]
    assert "::warning::W" in capsys.readouterr().out


def test_main_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GA4_SA_JSON", raising=False)
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    assert m.main(["--out", str(tmp_path / "x.jsonl")]) == 2
    assert m.main(["--property-id", "1", "--out", str(tmp_path / "x.jsonl")]) == 2
    assert not (tmp_path / "x.jsonl").exists()


def test_main_fails_loudly_on_api_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("400 bad dimension")
    monkeypatch.setattr(m, "fetch", boom)
    monkeypatch.setattr(m, "_load_credentials", lambda s: None)
    import google.analytics.data_v1beta as ga
    monkeypatch.setattr(ga, "BetaAnalyticsDataClient", lambda credentials=None: object())
    rc = m.main(["--property-id", "1", "--sa-json", "{}", "--out", str(tmp_path / "x.jsonl")])
    assert rc == 1
    assert not (tmp_path / "x.jsonl").exists()
