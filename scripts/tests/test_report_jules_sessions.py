"""Tests for scripts/report_jules_sessions.py の workflow 別内訳 (#2024)。

API は叩かず group_created_24h_by_wf の純粋ロジックのみ検証する。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from report_jules_sessions import UNTAGGED, group_created_24h_by_wf  # noqa: E402


def _iso(hours_ago: float) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_group_by_wf_prefix() -> None:
    sessions = [
        {"createTime": _iso(1), "title": "[wf03] 記事生成: 商品深掘りガイド"},
        {"createTime": _iso(2), "title": "[wf03] 記事生成: 商品深掘りガイド"},
        {"createTime": _iso(3), "title": "[wf14] narrative: タカラトミー (#731)"},
        {"createTime": _iso(4), "title": "engagement: X いろママ daily drafts (2026-06-09)"},
    ]
    groups = group_created_24h_by_wf(sessions)
    assert groups == {"wf03": 2, "wf14": 1, UNTAGGED: 1}


def test_group_by_wf_excludes_aged_out() -> None:
    # 24h を超えた session はゲート母数から外れるので内訳にも出ない
    sessions = [
        {"createTime": _iso(1), "title": "[wf35] sns-copy: product post drafts"},
        {"createTime": _iso(25), "title": "[wf35] sns-copy: product post drafts"},
    ]
    assert group_created_24h_by_wf(sessions) == {"wf35": 1}


def test_group_by_wf_handles_missing_title_and_time() -> None:
    sessions = [
        {"createTime": _iso(1)},          # title 欠落 → UNTAGGED
        {"title": "[wf29] engagement"},   # createTime 欠落 → 無視
        {"createTime": None, "title": "[wf33] x"},  # None → 無視
    ]
    assert group_created_24h_by_wf(sessions) == {UNTAGGED: 1}


# --- fetch_sessions の窓打ち切りと持ち時間 (#9410) ---------------------------------


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _fake_get(pages: list[list[dict]], calls: list[dict]):
    def _get(url, params=None, headers=None, timeout=None):
        idx = int((params or {}).get("pageToken") or 0)
        calls.append({"idx": idx, "timeout": timeout, "page_size": (params or {}).get("pageSize")})
        nxt = str(idx + 1) if idx + 1 < len(pages) else ""
        return _Resp({"sessions": pages[idx], "nextPageToken": nxt})
    return _get


def test_fetch_stops_at_window_when_descending(monkeypatch) -> None:
    from report_jules_sessions import fetch_sessions

    pages = [
        [{"createTime": _iso(1)}, {"createTime": _iso(30)}],
        [{"createTime": _iso(50)}, {"createTime": _iso(80)}],   # 最後が 72h より古い → ここで止まる
        [{"createTime": _iso(100)}],
    ]
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get(pages, calls)))
    got = fetch_sessions("k", window_hours=72)
    assert len(calls) == 2
    assert len(got) == 4


def test_fetch_reads_all_pages_when_not_descending(monkeypatch) -> None:
    """降順を確認できないときは打ち切らない (古い順・順不同で取りこぼさない)。"""
    from report_jules_sessions import fetch_sessions

    pages = [
        [{"createTime": _iso(100)}, {"createTime": _iso(90)}],
        [{"createTime": _iso(2)}],
    ]
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get(pages, calls)))
    got = fetch_sessions("k", window_hours=72)
    assert len(calls) == 2
    assert len(got) == 3


def test_fetch_without_window_reads_all_pages(monkeypatch) -> None:
    from report_jules_sessions import fetch_sessions

    pages = [[{"createTime": _iso(100)}], [{"createTime": _iso(200)}], [{"createTime": _iso(300)}]]
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get(pages, calls)))
    assert len(fetch_sessions("k")) == 3


def test_fetch_raises_when_deadline_exceeded(monkeypatch) -> None:
    """持ち時間を超えたら partial を返さず送出する。"""
    import pytest
    from report_jules_sessions import fetch_sessions

    pages = [[{"createTime": _iso(1)}], [{"createTime": _iso(2)}], [{"createTime": _iso(3)}]]
    calls: list[dict] = []
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get(pages, calls)))
    ticks = iter([0, 0, 50, 200, 400])
    with pytest.raises(TimeoutError):
        fetch_sessions("k", deadline=100, monotonic=lambda: next(ticks))
    assert len(calls) == 2


def test_default_page_fits_timeout_with_margin() -> None:
    """1 件あたり約 1 秒 (2026-10 実測) でも 1 ページが timeout に収まる既定値にしておく。"""
    import jules_quota_gate as gate
    import report_jules_sessions as rep

    for mod in (gate, rep):
        assert mod.DEFAULT_TIMEOUT >= 2 * mod.DEFAULT_PAGE_SIZE * 1.2
