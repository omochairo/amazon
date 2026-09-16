"""Tavily 月次予算の着手前チェック (#4841 V2 前提①)。

owner の上書き: 「着手時に台帳を読み直し、残り日数 × 実測ペース + 40 が 900 を
超えないことを確認してから走らせる。超えるなら止まって報告する」。
"""
from __future__ import annotations

import calendar
import json
import logging
import pathlib
import subprocess
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("search_query_trial.budget")

DEFAULT_USAGE_PATH = pathlib.Path("data/raw/per_asin/_tavily_usage.json")
DEFAULT_USAGE_REL_PATH = "data/raw/per_asin/_tavily_usage.json"
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


def _month_used_from_data(data: Any, now: datetime) -> int:
    month_label = now.strftime("%Y-%m")
    if isinstance(data, dict) and data.get("month") == month_label:
        calls = data.get("calls")
        if isinstance(calls, int) and calls >= 0:
            return calls
    return 0


def month_used(usage_path: pathlib.Path, now: datetime) -> int:
    """usage_path が今月分かどうかを見て、今月の消費件数を返す (月が違えば0)。"""
    return _month_used_from_data(_load(usage_path), now)


def refresh_ledger_from_origin_main(
    rel_path: str = DEFAULT_USAGE_REL_PATH,
    *, repo_root: pathlib.Path = pathlib.Path("."),
) -> Any:
    """origin/main を fetch し、共有台帳の最新内容を読む (owner 修正6)。

    ローカルの worktree の台帳は最大1日ぶん古くなりうる (前回 main へ反映されて
    以降に既存レーンが消費した分がここに乗らない)。着手直前に fetch し直す
    ことで、その分を見落とさないようにする。ワークツリーには書かない
    (`git show` で内容だけ読む)。

    fetch/show に失敗したら (ネットワーク不可のサンドボックス等) None を返す。
    呼び出し側はローカルの usage_path にフォールバックし、その旨をログ/報告に
    残すこと。
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"], cwd=repo_root, check=True,
            capture_output=True, text=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "show", f"origin/main:{rel_path}"], cwd=repo_root,
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("origin/main の台帳を取得できなかった — ローカルにフォールバック: %s", e)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("origin/main の台帳が JSON として読めない — ローカルにフォールバック: %s", e)
        return None


def check_tavily_budget(
    usage_path: pathlib.Path = DEFAULT_USAGE_PATH,
    *,
    monthly_budget: int = DEFAULT_MONTHLY_BUDGET,
    daily_pace: float = DEFAULT_DAILY_PACE,
    planned_queries: int = PLANNED_QUERIES,
    now: datetime | None = None,
    usage_data: Any = None,
) -> dict[str, Any]:
    """残り枠が V2 の消費予定 (既定40 query) を含めて月次予算を超えないかを判定する。

    「残り日数」は今日を含む (今日分もまだ既存レーンが消費する)。

    `usage_data` を渡すとそれを台帳として使う (owner 修正6:
    `refresh_ledger_from_origin_main` で取得した最新の内容を渡す想定)。
    省略時は従来どおり `usage_path` のローカルファイルを読む。
    """
    now = now or datetime.now(timezone.utc)
    used = _month_used_from_data(
        usage_data if usage_data is not None else _load(usage_path), now,
    )
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    remaining_days = days_in_month - now.day + 1
    projected_existing_lane = remaining_days * daily_pace
    projected_total = used + projected_existing_lane + planned_queries
    feasible = projected_total <= monthly_budget

    return {
        "usage_path": str(usage_path),
        "usage_source": "origin_main" if usage_data is not None else "local_file",
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
