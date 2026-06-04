"""report_jules_sessions.py — Jules API session の実状態を集計 (#1353)

`scripts/report_jules_usage.py` が gh の PR 件数から *推定* するのに対し、本スクリプトは
Jules API (`GET /v1alpha/sessions`) を直接叩いて **実セッションの state 分布**を取得する。

#1353 / #1599 の調査で判明した API の事実 (2026-06-04 session 97):
  - Jules Pro 上限: **daily 100 / concurrent 15** (rolling 24h window, fixed midnight reset ではない)
  - daily は「セッション *作成* 数」を rolling 24h で数える → 作成から 24h で自然に枠が空く
    (#1353 の「PT/UTC midnight でリセット」仮説は両方誤り。体感「リセットが遅い」の正体は rolling window)
  - **session DELETE は API に存在しない** (create/get/list/approvePlan/sendMessage のみ)
    → #1599 の「DELETE 自動化でログ退避→削除」は API 上不可能。スタック session は
      terminal (COMPLETED/FAILED) になるまで concurrent 枠を占有し続ける = 真の浪費ベクトル

このスクリプトは「枠の圧迫」を可視化する。daily 圧 (rolling 24h 作成数 / 100) と
concurrent 圧 (active session 数 / 15)、および AWAITING_USER_FEEDBACK 等で詰まった
スタック session の一覧を出す。caller (16-jules-daily-report.yml) が Issue にコメントする。

使い方:
    JULES_API_KEY=... python scripts/report_jules_sessions.py
    JULES_API_KEY=... python scripts/report_jules_sessions.py --stuck-hours 6
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

API_BASE = "https://jules.googleapis.com/v1alpha/sessions"

# Jules Pro plan limits (2026-06 時点)。plan 変更時はここを更新。
DAILY_CAP = 100
CONCURRENT_CAP = 15

# terminal でない = concurrent 枠を占有する state。
ACTIVE_STATES = {
    "QUEUED", "PLANNING", "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_FEEDBACK", "IN_PROGRESS", "PAUSED",
}
# 人手の介入なしには自力で進めず、concurrent 枠を無期限に占有しうる state。
STUCK_STATES = {"AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL", "PAUSED"}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_sessions(api_key: str, *, max_pages: int = 30) -> list[dict]:
    """LIST /v1alpha/sessions を pageToken で全ページ取得。max_pages で暴走防止。"""
    import requests  # 遅延 import (local lint で requests 無しでも import 可能に)

    sessions: list[dict] = []
    token: str | None = None
    for _ in range(max_pages):
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        r = requests.get(
            API_BASE,
            params=params,
            headers={"X-Goog-Api-Key": api_key},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        sessions.extend(body.get("sessions") or [])
        token = body.get("nextPageToken") or ""
        if not token:
            break
    return sessions


def _state_of(s: dict) -> str:
    # docs は `state`、03-invoke の monitor は `status` を読む。両対応。
    return (s.get("state") or s.get("status") or "STATE_UNSPECIFIED").upper()


def summarize(sessions: list[dict], *, stuck_hours: float) -> dict:
    now = _now()
    by_state: dict[str, int] = {}
    created_24h = 0
    active = 0
    stuck: list[dict] = []
    for s in sessions:
        st = _state_of(s)
        by_state[st] = by_state.get(st, 0) + 1
        created = _parse_ts(s.get("createTime"))
        if created and (now - created).total_seconds() <= 86400:
            created_24h += 1
        if st in ACTIVE_STATES:
            active += 1
        if st in STUCK_STATES:
            updated = _parse_ts(s.get("updateTime")) or created
            age_h = (now - updated).total_seconds() / 3600 if updated else 0.0
            if age_h >= stuck_hours:
                stuck.append({
                    "id": s.get("id") or (s.get("name") or "").split("/")[-1],
                    "title": (s.get("title") or "")[:50],
                    "state": st,
                    "age_h": age_h,
                })
    stuck.sort(key=lambda x: x["age_h"], reverse=True)
    return {
        "total": len(sessions),
        "by_state": by_state,
        "created_24h": created_24h,
        "active": active,
        "stuck": stuck,
    }


def _pct_emoji(used: int, cap: int) -> str:
    if cap <= 0:
        return ""
    r = used / cap
    if r >= 1.0:
        return "🔴 上限到達"
    if r >= 0.8:
        return "🟡 接近"
    return "🟢 OK"


def render(summary: dict, *, stuck_hours: float) -> str:
    L: list[str] = []
    date = _now().strftime("%Y-%m-%d %H:%M")
    L.append(f"## Jules sessions (live API) — {date} UTC")
    L.append("")
    d, a = summary["created_24h"], summary["active"]
    L.append(f"- **daily 圧 (rolling 24h 作成): {d} / {DAILY_CAP}** {_pct_emoji(d, DAILY_CAP)}")
    L.append(f"- **concurrent 圧 (active 枠占有): {a} / {CONCURRENT_CAP}** {_pct_emoji(a, CONCURRENT_CAP)}")
    L.append(f"- total visible sessions: {summary['total']}")
    L.append("")
    L.append("### state 分布")
    L.append("")
    L.append("| state | count |")
    L.append("|---|---:|")
    for st, n in sorted(summary["by_state"].items(), key=lambda x: -x[1]):
        L.append(f"| {st} | {n} |")
    L.append("")
    stuck = summary["stuck"]
    L.append(f"### スタック session (>{stuck_hours:g}h, concurrent 枠を占有)")
    L.append("")
    if not stuck:
        L.append("_なし — concurrent 枠の無駄な占有は検出されず_")
    else:
        L.append("⚠️ DELETE は API 非対応。terminal 化には sendMessage/approvePlan か Jules 側の timeout 待ち。")
        L.append("")
        L.append("| id | state | age(h) | title |")
        L.append("|---|---|---:|---|")
        for x in stuck[:20]:
            L.append(f"| `{x['id']}` | {x['state']} | {x['age_h']:.1f} | {x['title']} |")
    L.append("")
    L.append("_Generated by `scripts/report_jules_sessions.py` via `16-jules-daily-report.yml`._")
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--stuck-hours", type=float, default=6.0,
                   help="この時間以上更新の無い AWAITING_* / PAUSED をスタック扱い")
    args = p.parse_args()
    api_key = os.environ.get("JULES_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("JULES_API_KEY 未設定\n")
        print("## Jules sessions (live API)\n\n_JULES_API_KEY 未設定のためスキップ_")
        return 0
    try:
        sessions = fetch_sessions(api_key)
    except Exception as e:  # noqa: BLE001 — レポート用途なので握り潰して継続
        sys.stderr.write(f"fetch failed: {e}\n")
        print(f"## Jules sessions (live API)\n\n_API 取得失敗: {e}_")
        return 0
    print(render(summarize(sessions, stuck_hours=args.stuck_hours), stuck_hours=args.stuck_hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
