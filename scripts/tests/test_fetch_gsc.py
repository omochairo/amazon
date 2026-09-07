"""scripts/fetch_gsc.py unit tests (#3988 B-1: site-wide totals)."""
from __future__ import annotations

import pytest

import scripts.fetch_gsc as fetch_gsc_module
from scripts.fetch_gsc import (API_ROW_LIMIT_MAX, TOP_PAGE_DEFAULT,
                              TOP_QUERY_DEFAULT, _query, fetch)


# ---------------------------------------------------------------------------
# fake googleapiclient searchanalytics().query(siteUrl=..., body=...).execute()
# chain
# ---------------------------------------------------------------------------

class _FakeExecute:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def execute(self) -> dict:
        return {"rows": self._rows}


class _FakeSearchAnalytics:
    def __init__(self, calls: list[dict], response_queue: list[list[dict]]):
        self._calls = calls
        self._response_queue = response_queue

    def query(self, siteUrl: str, body: dict):
        self._calls.append({"siteUrl": siteUrl, "body": body})
        rows = self._response_queue.pop(0) if self._response_queue else []
        return _FakeExecute(rows)


class FakeService:
    """service.searchanalytics().query(...).execute() chain, records each body."""

    def __init__(self, responses: list[list[dict]]):
        self.calls: list[dict] = []
        self._response_queue = list(responses)

    def searchanalytics(self):
        return _FakeSearchAnalytics(self.calls, self._response_queue)


def _page_row(page: str, clicks: int, impressions: int, position: float = 5.0) -> dict:
    return {
        "keys": [page],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0.0,
        "position": position,
    }


def _query_row(query: str, clicks: int, impressions: int, position: float = 5.0) -> dict:
    return {
        "keys": [query],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0.0,
        "position": position,
    }


def _sitewide_row(clicks: int, impressions: int, ctr: float, position: float) -> dict:
    # dimensionless row: real API omits "keys" (or returns []); our normalizer never
    # touches "keys" when dims is empty, so we deliberately don't set it here.
    return {"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


def _patch_service(monkeypatch, responses: list[list[dict]]) -> FakeService:
    fake = FakeService(responses)
    monkeypatch.setattr(fetch_gsc_module, "_build_service", lambda *a, **kw: fake)
    return fake


# ---------------------------------------------------------------------------
# aggregationType (omochairo/omcha-ops#101)
#
# 1 日窓では query 次元が 5,000 行ちょうどで打ち切られる。byPage を付けると
# 外れる代わりに impressions の意味が変わるので、**どちらで取ったかをファイルに
# 残す**。残していないと、混ざった時系列の段差を後から説明できない。
# ---------------------------------------------------------------------------


def test_query_omits_aggregation_type_by_default():
    fake = FakeService([[_query_row("q", 1, 10)], []])
    _query(fake, "sc-domain:example.com", "2026-09-04", "2026-09-04", ["query"], 100)
    assert "aggregationType" not in fake.calls[0]["body"]


def test_query_sends_aggregation_type_when_given():
    fake = FakeService([[_query_row("q", 1, 10)], []])
    _query(fake, "sc-domain:example.com", "2026-09-04", "2026-09-04", ["query"], 100,
           "byPage")
    assert fake.calls[0]["body"]["aggregationType"] == "byPage"


def test_fetch_records_aggregation_type_in_payload(monkeypatch):
    fake = _patch_service(monkeypatch, _fetch_responses([], [], [], [], []))
    r = fetch("sc-domain:example.com", "cid", "cs", "rt", days=1, delay=3,
              end_date="2026-09-04", start_date="2026-09-04",
              aggregation_type="byPage")
    assert r["aggregation_type"] == "byPage"
    # 全ての系列に同じ集計方式を渡す (系列ごとに違うと totals が突き合わない)
    assert all(c["body"]["aggregationType"] == "byPage" for c in fake.calls)


def test_fetch_records_none_when_not_given(monkeypatch):
    _patch_service(monkeypatch, _fetch_responses([], [], [], [], []))
    r = fetch("sc-domain:example.com", "cid", "cs", "rt", days=1, delay=3,
              end_date="2026-09-04")
    assert r["aggregation_type"] is None


# ---------------------------------------------------------------------------
# fetch(): 窓の日数 (omochairo/omcha-ops#101)
#
# GSC の startDate/endDate は両端を含むので --days 1 は 2 日窓になる。日次
# アーカイブはこれを 1 日窓だと思って貯めており、隣り合うファイルが 1 日ずつ
# 重なっていた。--start-date で始端を明示できることと、--days の意味を変えて
# いないこと (既存レーンの窓を黙って動かさない) の両方を固定する。
# ---------------------------------------------------------------------------


def test_fetch_days_1_is_still_a_two_day_window(monkeypatch):
    """--days の意味は据え置き。navi の日次/週次レーンの窓を黙って変えない。"""
    _patch_service(monkeypatch, _fetch_responses([], [], [], [], []))
    r = fetch("sc-domain:example.com", "cid", "cs", "rt", days=1, delay=3,
              end_date="2026-09-04")
    assert r["range"]["start"] == "2026-09-03"
    assert r["range"]["end"] == "2026-09-04"
    assert r["range"]["window_days"] == 2


def test_fetch_start_date_gives_a_true_single_day_window(monkeypatch):
    fake = _patch_service(monkeypatch, _fetch_responses([], [], [], [], []))
    r = fetch("sc-domain:example.com", "cid", "cs", "rt", days=1, delay=3,
              end_date="2026-09-04", start_date="2026-09-04")
    assert r["range"]["start"] == "2026-09-04"
    assert r["range"]["end"] == "2026-09-04"
    assert r["range"]["window_days"] == 1
    # API に渡る body も 1 日窓であること (range だけ直っていても意味がない)
    assert fake.calls[0]["body"]["startDate"] == "2026-09-04"
    assert fake.calls[0]["body"]["endDate"] == "2026-09-04"


def test_fetch_rejects_start_after_end(monkeypatch):
    _patch_service(monkeypatch, _fetch_responses([], [], [], [], []))
    with pytest.raises(SystemExit):
        fetch("sc-domain:example.com", "cid", "cs", "rt", days=1, delay=3,
              end_date="2026-09-04", start_date="2026-09-05")


# ---------------------------------------------------------------------------
# _query(): startRow pagination (omochairo/omcha-ops#101 1d)
#
# API の 1 リクエスト上限は 25,000 行。それを超える系列 (by_combo は実測で既に
# 上限の 95%) を単発リクエストで取ると、超えたぶんが「取れない」ではなく黙って
# 切られる。ページングしていることを、body の startRow で確かめる。
# ---------------------------------------------------------------------------


def test_query_pages_with_start_row_when_over_api_limit():
    # 1 ページ目が満杯 -> 2 ページ目を取りに行く
    first = [_query_row(f"q{i}", 1, 10) for i in range(API_ROW_LIMIT_MAX)]
    second = [_query_row("tail", 1, 10)]
    fake = FakeService([first, second])

    rows = _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01",
                  ["query"], API_ROW_LIMIT_MAX + 5000)

    assert len(rows) == API_ROW_LIMIT_MAX + 1
    assert rows[-1]["query"] == "tail"
    assert len(fake.calls) == 2
    assert fake.calls[0]["body"]["startRow"] == 0
    assert fake.calls[0]["body"]["rowLimit"] == API_ROW_LIMIT_MAX
    assert fake.calls[1]["body"]["startRow"] == API_ROW_LIMIT_MAX
    # 残り 5000 しか要求しない (row_limit を超えて取らない)
    assert fake.calls[1]["body"]["rowLimit"] == 5000


def test_query_stops_when_page_not_full():
    # 返りが要求より少なければ次ページは引かない (無駄な API コールを撃たない)
    fake = FakeService([[_query_row("only", 1, 10)]])

    rows = _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01",
                  ["query"], API_ROW_LIMIT_MAX)

    assert len(rows) == 1
    assert len(fake.calls) == 1


def test_query_does_not_exceed_row_limit():
    # row_limit が API 上限以下なら 1 発で終わる (従来の挙動)
    fake = FakeService([[_query_row(f"q{i}", 1, 10) for i in range(100)]])

    rows = _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01",
                  ["query"], 100)

    assert len(rows) == 100
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# _query(): dimensions key omission (1a)
# ---------------------------------------------------------------------------

def test_query_dimensionless_omits_dimensions_key():
    fake = FakeService([[_sitewide_row(10, 100, 0.1, 4.0)]])
    _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01", [], 1)
    body = fake.calls[0]["body"]
    assert "dimensions" not in body
    assert body["rowLimit"] == 1
    assert body["startDate"] == "2026-07-01"
    assert body["endDate"] == "2026-07-01"


def test_query_dimensioned_includes_dimensions_key():
    fake = FakeService([[_page_row("/foo/", 1, 10)]])
    _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01", ["page"], 100)
    body = fake.calls[0]["body"]
    assert body["dimensions"] == ["page"]


def test_query_dimensionless_row_normalizes_to_empty_dim_dict():
    """dims=[] のとき row 正規化は {} に degrade する (既存の内包表記のまま安全)。"""
    fake = FakeService([[_sitewide_row(10, 100, 0.1, 4.0)]])
    rows = _query(fake, "sc-domain:example.com", "2026-07-01", "2026-07-01", [], 1)
    assert len(rows) == 1
    assert rows[0]["clicks"] == 10
    assert rows[0]["impressions"] == 100
    assert rows[0]["ctr"] == 0.1
    assert rows[0]["position"] == 4.0
    # no dimension keys leaked into the row
    assert set(rows[0].keys()) == {"clicks", "impressions", "ctr", "position"}


# ---------------------------------------------------------------------------
# fetch(): site-wide totals (1b)
# ---------------------------------------------------------------------------

def _fetch_responses(by_query_rows, by_page_rows, by_combo_rows, by_device_rows, sitewide_rows):
    # fetch() 呼び出し順: by_query, by_page, by_combo, by_device, sitewide
    return [by_query_rows, by_page_rows, by_combo_rows, by_device_rows, sitewide_rows]


def test_fetch_populates_sitewide_values_from_dimensionless_response(monkeypatch):
    responses = _fetch_responses(
        by_query_rows=[_query_row("knick knack", 3, 30)],
        by_page_rows=[_page_row("/a/", 3, 30)],
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(clicks=5000, impressions=90000, ctr=0.0556, position=12.3)],
    )
    fake = _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")

    t = result["totals"]
    assert t["clicks_sitewide"] == 5000
    assert t["impressions_sitewide"] == 90000
    assert t["ctr_sitewide"] == 0.0556
    assert t["position_sitewide"] == 12.3

    # the last recorded call is the dimensionless site-wide query
    last_body = fake.calls[-1]["body"]
    assert "dimensions" not in last_body
    assert last_body["rowLimit"] == 1


def test_fetch_sitewide_none_when_dimensionless_response_has_no_rows(monkeypatch):
    responses = _fetch_responses(
        by_query_rows=[],
        by_page_rows=[],
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[],  # no data for this day
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")

    t = result["totals"]
    assert t["clicks_sitewide"] is None
    assert t["impressions_sitewide"] is None
    assert t["ctr_sitewide"] is None
    assert t["position_sitewide"] is None
    # truncated flags are still real booleans, not None
    assert t["truncated_pages"] is False
    assert t["truncated_queries"] is False


def test_fetch_truncated_pages_true_at_cap(monkeypatch):
    by_page_rows = [_page_row(f"/p{i}/", 1, 10) for i in range(TOP_PAGE_DEFAULT)]
    responses = _fetch_responses(
        by_query_rows=[],
        by_page_rows=by_page_rows,
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(1, 10, 0.1, 5.0)],
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")
    assert result["totals"]["truncated_pages"] is True


def test_fetch_truncated_pages_false_under_cap(monkeypatch):
    by_page_rows = [_page_row("/p0/", 1, 10)]
    responses = _fetch_responses(
        by_query_rows=[],
        by_page_rows=by_page_rows,
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(1, 10, 0.1, 5.0)],
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")
    assert result["totals"]["truncated_pages"] is False


def test_fetch_truncated_queries_true_at_cap(monkeypatch):
    by_query_rows = [_query_row(f"q{i}", 1, 10) for i in range(TOP_QUERY_DEFAULT)]
    responses = _fetch_responses(
        by_query_rows=by_query_rows,
        by_page_rows=[],
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(1, 10, 0.1, 5.0)],
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")
    assert result["totals"]["truncated_queries"] is True


def test_fetch_truncated_queries_false_under_cap(monkeypatch):
    by_query_rows = [_query_row("q0", 1, 10)]
    responses = _fetch_responses(
        by_query_rows=by_query_rows,
        by_page_rows=[],
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(1, 10, 0.1, 5.0)],
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")
    assert result["totals"]["truncated_queries"] is False


def test_fetch_clicks_and_impressions_sum_are_top_n_page_sums(monkeypatch):
    """regression guard: clicks_sum/impressions_sum の意味は変えていない
    (by_page の合計のまま)。"""
    by_page_rows = [
        _page_row("/a/", clicks=3, impressions=30),
        _page_row("/b/", clicks=7, impressions=70),
        _page_row("/c/", clicks=2, impressions=20),
    ]
    responses = _fetch_responses(
        by_query_rows=[],
        by_page_rows=by_page_rows,
        by_combo_rows=[],
        by_device_rows=[],
        sitewide_rows=[_sitewide_row(999, 9999, 0.5, 1.0)],  # deliberately different
    )
    _patch_service(monkeypatch, responses)

    result = fetch("sc-domain:example.com", "cid", "secret", "refresh",
                    days=1, delay=0, end_date="2026-07-20")
    t = result["totals"]
    assert t["clicks_sum"] == 3 + 7 + 2
    assert t["impressions_sum"] == 30 + 70 + 20
    # sitewide values are independent and NOT equal to the page sums here
    assert t["clicks_sitewide"] == 999
    assert t["impressions_sitewide"] == 9999
