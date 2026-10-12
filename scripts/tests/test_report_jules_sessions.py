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
    """1 リクエスト約 1 分 (2026-10 実測) の倍は待つ既定値にしておく。"""
    import jules_quota_gate as gate
    import report_jules_sessions as rep

    for mod in (gate, rep):
        assert mod.DEFAULT_TIMEOUT >= 2 * 57


# --- PR が出なかった記事生成セッション (#9410) -------------------------------------


def _sess(sid: str, state: str, *, title: str = "[wf03] 記事生成: B0TEST0001",
          hours_ago: float = 5, outputs=None) -> dict:
    s = {"id": sid, "state": state, "title": title, "createTime": _iso(hours_ago)}
    if outputs is not None:
        s["outputs"] = outputs
    return s


def test_no_pr_sessions_splits_by_outcome() -> None:
    from report_jules_sessions import no_pr_sessions

    pr = [{"pullRequest": {"url": "https://example.invalid/pr/1"}}]
    info = no_pr_sessions([
        _sess("1", "COMPLETED", outputs=pr),
        _sess("2", "COMPLETED"),                                    # 空振り
        _sess("3", "COMPLETED", outputs=[{"changeSet": {}}]),       # 変更はあるが PR 無し
        _sess("4", "FAILED", hours_ago=1),
        _sess("5", "AWAITING_USER_FEEDBACK"),
        _sess("6", "IN_PROGRESS"),                                  # 実行中は数えない
        _sess("7", "FAILED", title="[wf33] news"),                  # 03 以外は対象外
    ])
    assert info["with_pr"] == 1 and info["running"] == 1 and info["outputs_seen"]
    assert [r["id"] for r in info["rows"]] == ["4", "2", "3", "5"]
    assert {r["id"]: r["outputs"] for r in info["rows"]}["3"] == ["changeSet"]


def test_render_no_pr_refuses_to_judge_without_outputs() -> None:
    """LIST が outputs を返さないとき、全部を PR 無しと書かない。"""
    from report_jules_sessions import no_pr_sessions, render_no_pr

    info = no_pr_sessions([_sess("1", "COMPLETED"), _sess("2", "COMPLETED")])
    assert not info["outputs_seen"]
    assert "判定できない" in render_no_pr(info)
    assert "| `1` |" not in render_no_pr(info)


def test_activity_digest_exposes_only_kinds_and_failure_reason() -> None:
    from report_jules_sessions import activity_digest

    d = activity_digest([
        {"createTime": "2026-10-12T10:31:00Z", "sessionFailed": {"reason": "boom | x\ny"}},
        {"createTime": "2026-10-12T10:27:00Z", "planGenerated": {"plan": {"steps": ["SECRET"]}}},
        {"createTime": "2026-10-12T10:28:00Z", "description": "d",
         "agentMessaged": {"agentMessage": "PRIVATE PROMPT TEXT"}},
    ])
    assert d == {"n": 3, "kinds": ["planGenerated", "agentMessaged", "sessionFailed"],
                 "reason": "boom / x y"}
    assert "PRIVATE" not in str(d) and "SECRET" not in str(d)


def test_add_activity_digests_is_best_effort_and_limited() -> None:
    from report_jules_sessions import add_activity_digests, render_no_pr

    rows = [{"id": str(i), "title": "t", "state": "FAILED", "age_h": i, "reason": ""} for i in range(4)]
    seen: list[str] = []

    def _fetch(_key, sid):
        seen.append(sid)
        if sid == "1":
            raise RuntimeError("timeout")
        return [{"createTime": "2026-10-12T10:31:00Z", "sessionFailed": {"reason": "r"}}]

    add_activity_digests("k", rows, limit=3, fetch=_fetch)
    assert seen == ["0", "1", "2"]
    assert "digest" in rows[0] and "digest" not in rows[1] and "digest" not in rows[3]
    out = render_no_pr({"rows": rows, "with_pr": 2, "running": 0, "outputs_seen": True})
    assert "PR あり: 2 / 実行中: 0 / PR 無しで終了: 4 (FAILED 4)" in out
    assert "| sessionFailed | r |" in out
