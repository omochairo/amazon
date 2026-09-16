"""Tavily 月次予算の着手前チェック (#4841 V2 前提①)。

owner の上書き: 「着手時に台帳を読み直し、残り日数 × 実測ペース + 40 が 900 を
超えないことを確認してから走らせる。超えるなら止まって報告する」。
"""
from __future__ import annotations

import calendar
import json
import pathlib
from datetime import datetime, timezone
from typing import Any

DEFAULT_USAGE_PATH = pathlib.Path("data/raw/per_asin/_tavily_usage.json")
DEFAULT_MONTHLY_BUDGET = 900
# 2026-09-16 時点の実測ペース (#4841 owner 実測: 374回/9-14時点、既存レーンの
# 実消費ペース)。日によって変わるので呼び出し側で上書きできる。
DEFAULT_DAILY_PACE = 27.0
PLANNED_QUERIES = 40  # V2 が新たに使う query 数 (Q1 20 + Q2 20)


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def month_used(usage_path: pathlib.Path, now: datetime) -> int:
    """usage_path が今月分かどうかを見て、今月の消費件数を返す (月が違えば0)。"""
    data = _load(usage_path)
    month_label = now.strftime("%Y-%m")
    if isinstance(data, dict) and data.get("month") == month_label:
        calls = data.get("calls")
        if isinstance(calls, int) and calls >= 0:
            return calls
    return 0


def check_tavily_budget(
    usage_path: pathlib.Path = DEFAULT_USAGE_PATH,
    *,
    monthly_budget: int = DEFAULT_MONTHLY_BUDGET,
    daily_pace: float = DEFAULT_DAILY_PACE,
    planned_queries: int = PLANNED_QUERIES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """残り枠が V2 の消費予定 (既定40 query) を含めて月次予算を超えないかを判定する。

    「残り日数」は今日を含む (今日分もまだ既存レーンが消費する)。
    """
    now = now or datetime.now(timezone.utc)
    used = month_used(usage_path, now)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    remaining_days = days_in_month - now.day + 1
    projected_existing_lane = remaining_days * daily_pace
    projected_total = used + projected_existing_lane + planned_queries
    feasible = projected_total <= monthly_budget

    return {
        "usage_path": str(usage_path),
        "used_this_month": used,
        "monthly_budget": monthly_budget,
        "daily_pace": daily_pace,
        "remaining_days_in_month": remaining_days,
        "planned_queries": planned_queries,
        "projected_existing_lane_remainder": round(projected_existing_lane, 1),
        "projected_total": round(projected_total, 1),
        "feasible": feasible,
        "checked_at": now.isoformat(),
    }
