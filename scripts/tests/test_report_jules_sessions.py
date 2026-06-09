"""Tests for scripts/report_jules_sessions.py の workflow 別内訳 (#2024)。

API は叩かず group_created_24h_by_wf の純粋ロジックのみ検証する。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

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
