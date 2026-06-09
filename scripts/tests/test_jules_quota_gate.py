"""Tests for scripts/jules_quota_gate.py (session 114 真因修正)。

実 session 作成数 (rolling-24h) ベースで 1 run の作成予算を返すゲートの純粋ロジック検証。
API は叩かず count_created_24h / remaining_budget のみをテストする。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from jules_quota_gate import count_created_24h, remaining_budget  # noqa: E402


def _iso(hours_ago: float) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_count_created_24h_window() -> None:
    sessions = [
        {"createTime": _iso(1)},    # in window
        {"createTime": _iso(23)},   # in window
        {"createTime": _iso(25)},   # aged out
        {"createTime": _iso(100)},  # aged out
        {"createTime": None},       # missing → 無視
        {},                         # createTime 欠落 → 無視
    ]
    assert count_created_24h(sessions) == 2


def test_remaining_budget_plenty() -> None:
    # 作成 10 / cap 80 → 残り 70 だが per-run max 12 でクランプ
    assert remaining_budget(10, cap=80, per_run_max=12) == 12


def test_remaining_budget_near_cap() -> None:
    # 作成 77 / cap 80 → 残り 3 (per-run max 未満なのでそのまま)
    assert remaining_budget(77, cap=80, per_run_max=12) == 3


def test_remaining_budget_exhausted() -> None:
    # 作成 80 / cap 80 → 0
    assert remaining_budget(80, cap=80, per_run_max=12) == 0


def test_remaining_budget_over_cap_clamps_to_zero() -> None:
    # 旧バグ状況 (実 100 作成) でも負数を返さず 0
    assert remaining_budget(100, cap=80, per_run_max=12) == 0
