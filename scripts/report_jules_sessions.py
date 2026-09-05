"""report_jules_sessions.py — Jules API session の実状態を集計 (#1353)

`scripts/report_jules_usage.py` が gh の PR 件数から *推定* するのに対し、本スクリプトは
Jules API (`GET /v1alpha/sessions`) を直接叩いて **実セッションの state 分布**を取得する。

#1353 / #1599 の調査で判明した API の事実 (2026-06-04 session 97):
  - Jules Pro 上限: **daily 100 / concurrent 15** (rolling 24h window, fixed midnight reset ではない)
  - daily は「セッション *作成* 数」を rolling 24h で数える → 作成から 24h で自然に枠が空く
    (#1353 の「PT/UTC midnight でリセット」仮説は両方誤り。体感「リセットが遅い」の正体は rolling window)
  - **session DELETE は API に存在しない** (create/get/list/approvePlan/sendMessage のみ)
  - concurrent 15 を食うのは実働中 (QUEUED/PLANNING/IN_PROGRESS) のみ。AWAITING_*/PAUSED の
    人間待ち idle は concurrent を消費しない (2026-06-04 実測: 実働13+質問5=18 alive>cap15 許容)。
    → 質問セッションは放置で OK (concurrent 非占有 + daily は 24h で aging out)。手動削除不要。
      残コストは「記事が生成されない content gap」のみ = 予防 (#1600) の対象。

このスクリプトは「枠の圧迫」を可視化する。daily 圧 (rolling 24h 作成数 / 100)、
concurrent 圧 (実working / 15)、人間待ち放置数、AWAITING_USER_FEEDBACK 等の一覧を出す。
加えて #2024 で「直近 24h に作成され FAILED で終わった session の一覧 + 理由」を出す
(quota か否かを毎回逆算調査しなくて済むように。FAILED は作成後の実行時失敗で quota 非関連)。
caller (16-jules-daily-report.yml) が Issue にコメントする。

使い方:
    JULES_API_KEY=... python scripts/report_jules_sessions.py
    JULES_API_KEY=... python scripts/report_jules_sessions.py --stuck-hours 6
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys

API_BASE = "https://jules.googleapis.com/v1alpha/sessions"

# create 系 workflow が session title 先頭に付ける machine-readable prefix `[wfNN]`
# (#2024)。rolling-24h の作成数を workflow 別に group-by して「誰が枠を食ったか」を
# 可視化する。prefix を付けない非ゲート workflow / 旧 session は UNTAGGED に集計。
_WF_PREFIX_RE = re.compile(r"^\[(wf\d{2})\]")
UNTAGGED = "(prefix なし)"
# 内訳表の可読ラベル。prefix を増やしたらここも追記。
WF_LABELS = {
    "wf03": "記事生成 (商品深掘り)",
    "wf14": "ブランド narrative",
    "wf29": "engagement refill",
    "wf33": "engagement news",
    "wf35": "sns-copy",
}

# Jules Pro plan limits (2026-06 時点)。plan 変更時はここを更新。
DAILY_CAP = 100
CONCURRENT_CAP = 15

# concurrent 15 枠を実際に消費するのは「実働中」state のみ。
# 2026-06-04 dashboard 実測: 実働13 + 質問5 = 18 alive が cap15 を超えても許容された
# → 人間待ち idle state (AWAITING_*/PAUSED) は concurrent 枠を消費しない。
CONCURRENT_STATES = {"QUEUED", "PLANNING", "IN_PROGRESS"}
# 人間待ちで自走しない state。concurrent も daily(24h で aging out) も食わないので
# quota 上は放置で問題ないが、その ASIN の記事が生成されないまま残る (content gap)。
WAITING_STATES = {"AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL", "PAUSED"}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# 2026-09-05: pageSize=100 / timeout=30s / retry 無しでは LIST /sessions が恒常的に
# read timeout し、日次レポートの「Jules sessions (live API)」が丸ごと欠測していた
# (08-24 断続 → 09-02 恒常)。stdlib 版の jules_quota_gate と同じ症状・同じ打ち手なので
# 既定値も揃える。ここが欠測すると quota 圧が見えなくなるだけだが、ゲート側は生成本数
# が落ちるので、両方直さないと「落ちていることに気づく経路」が残らない。
DEFAULT_PAGE_SIZE = int(os.environ.get("JULES_API_PAGE_SIZE", "50"))
DEFAULT_TIMEOUT = float(os.environ.get("JULES_API_TIMEOUT", "60"))
DEFAULT_ATTEMPTS = int(os.environ.get("JULES_API_ATTEMPTS", "3"))
DEFAULT_BACKOFF = float(os.environ.get("JULES_API_BACKOFF", "3"))


def fetch_sessions(
    api_key: str,
    *,
    max_pages: int = 30,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    sleep=None,
) -> list[dict]:
    """LIST /v1alpha/sessions を pageToken で全ページ取得。max_pages で暴走防止。

    一過性の timeout / 5xx は指数バックオフで retry する。retry を使い切ったら partial を
    返さず送出する (欠測を「session が少ない」と読み違えないため)。
    """
    import requests  # 遅延 import (local lint で requests 無しでも import 可能に)

    if sleep is None:
        import time as _time

        sleep = _time.sleep
    attempts = max(1, attempts)
    sessions: list[dict] = []
    token: str | None = None
    for _ in range(max_pages):
        params: dict = {"pageSize": page_size}
        if token:
            params["pageToken"] = token
        body = None
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                r = requests.get(
                    API_BASE,
                    params=params,
                    headers={"X-Goog-Api-Key": api_key},
                    timeout=timeout,
                )
                r.raise_for_status()
                body = r.json()
                break
            except Exception as e:  # noqa: BLE001 — timeout / 5xx / 429 を一律 retry
                last_exc = e
                if i + 1 < attempts:
                    wait = backoff * (2 ** i)
                    sys.stderr.write(
                        f"fetch_sessions: page 取得失敗 ({e}) → {wait:.0f}s 後に retry "
                        f"({i + 2}/{attempts})\n"
                    )
                    sleep(wait)
        if body is None:
            raise last_exc if last_exc is not None else RuntimeError("fetch failed")
        sessions.extend(body.get("sessions") or [])
        token = body.get("nextPageToken") or ""
        if not token:
            break
    return sessions


def _wf_key(s: dict) -> str:
    """session title 先頭の `[wfNN]` prefix を抽出。無ければ UNTAGGED。"""
    m = _WF_PREFIX_RE.match((s.get("title") or "").strip())
    return m.group(1) if m else UNTAGGED


def group_created_24h_by_wf(sessions: list[dict], *, now: _dt.datetime | None = None) -> dict[str, int]:
    """rolling-24h に作成された session を title の `[wfNN]` prefix で group-by (#2024)。

    overshoot 日に「どの create workflow が rolling-24h 枠を食い潰したか」を特定するための内訳。
    aging out した (24h 超) session は除外し、ゲートが見るのと同じ母数で数える。
    """
    ref = now or _now()
    groups: dict[str, int] = {}
    for s in sessions:
        created = _parse_ts(s.get("createTime"))
        if not (created and (ref - created).total_seconds() <= 86400):
            continue
        key = _wf_key(s)
        groups[key] = groups.get(key, 0) + 1
    return groups


def _state_of(s: dict) -> str:
    # docs は `state`、03-invoke の monitor は `status` を読む。両対応。
    return (s.get("state") or s.get("status") or "STATE_UNSPECIFIED").upper()


# session が FAILED で終わったときに「なぜか」を運用者へ即返すための probe (#2024)。
# Jules の LIST 応答に失敗理由がどのフィールドで載るかは plan/version で揺れるため、
# 既知・推測される複数フィールドを順に拾い、最初に見つかった非空文字列を短縮して返す。
# どれも無ければ空文字 (= ダッシュボードの activity ログ参照を促す)。
# 追加 API 呼び出しはしない (report の堅牢性優先・list 応答だけで完結)。
_REASON_FIELDS = (
    "failureReason", "failure_reason", "errorMessage", "error_message",
    "statusMessage", "status_message", "stateReason", "state_reason",
)


def _failure_reason(s: dict) -> str:
    """FAILED session の理由を list 応答内の既知フィールドから best-effort 抽出。"""
    # 1) error が Status object ({code,message,...}) または文字列のケース。
    err = s.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("status")
        if msg:
            return str(msg)
    elif isinstance(err, str) and err.strip():
        return err.strip()
    # 2) フラットな理由フィールド群。
    for k in _REASON_FIELDS:
        v = s.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def summarize(sessions: list[dict], *, stuck_hours: float) -> dict:
    now = _now()
    by_state: dict[str, int] = {}
    created_24h = 0
    active = 0
    waiting = 0
    stuck: list[dict] = []
    failed_24h: list[dict] = []
    for s in sessions:
        st = _state_of(s)
        by_state[st] = by_state.get(st, 0) + 1
        created = _parse_ts(s.get("createTime"))
        if created and (now - created).total_seconds() <= 86400:
            created_24h += 1
            # #2024: 直近 24h に *作成* され FAILED で終わったものを別建てで拾う。
            # 「quota か?」と運用者を毎回悩ませないため、どの session がいつ・なぜ落ちたかを
            # 即一覧化する (rolling-24h 窓 = 当日分の失敗を確実に捕捉)。aging out した古い
            # FAILED は除外し by_state の総数とは母数を分ける。age は updateTime (≒失敗時刻) 起点。
            if st == "FAILED":
                updated = _parse_ts(s.get("updateTime")) or created
                failed_24h.append({
                    "id": s.get("id") or (s.get("name") or "").split("/")[-1],
                    "title": (s.get("title") or "")[:50],
                    "age_h": (now - updated).total_seconds() / 3600 if updated else 0.0,
                    "reason": _failure_reason(s),
                })
        if st in CONCURRENT_STATES:
            active += 1
        if st in WAITING_STATES:
            waiting += 1
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
    failed_24h.sort(key=lambda x: x["age_h"])
    return {
        "total": len(sessions),
        "by_state": by_state,
        "created_24h": created_24h,
        "created_24h_by_wf": group_created_24h_by_wf(sessions, now=now),
        "active": active,
        "waiting": waiting,
        "stuck": stuck,
        "failed_24h": failed_24h,
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
    d, a, w = summary["created_24h"], summary["active"], summary["waiting"]
    L.append(f"- **daily 圧 (rolling 24h 作成): {d} / {DAILY_CAP}** {_pct_emoji(d, DAILY_CAP)}")
    L.append(f"- **concurrent 圧 (実working 枠占有: QUEUED/PLANNING/IN_PROGRESS): {a} / {CONCURRENT_CAP}** {_pct_emoji(a, CONCURRENT_CAP)}")
    L.append(f"- 人間待ち放置 (AWAITING_*/PAUSED): {w} _(quota 非占有・記事未生成)_")
    L.append(f"- total visible sessions: {summary['total']}")
    L.append("")
    L.append("### state 分布")
    L.append("")
    L.append("| state | count |")
    L.append("|---|---:|")
    for st, n in sorted(summary["by_state"].items(), key=lambda x: -x[1]):
        L.append(f"| {st} | {n} |")
    L.append("")
    L.append("### rolling-24h 作成の workflow 別内訳 (#2024)")
    L.append("")
    by_wf = summary.get("created_24h_by_wf") or {}
    if not by_wf:
        L.append("_直近 24h の作成なし_")
    else:
        total = summary["created_24h"] or 1
        L.append("どの create workflow が daily 枠を食ったか (overshoot 主因特定用)。"
                 f"`{UNTAGGED}` はゲート外 workflow / prefix 未付与の旧 session。")
        L.append("")
        L.append("| workflow | 用途 | 作成数 | 占有率 |")
        L.append("|---|---|---:|---:|")
        for key, n in sorted(by_wf.items(), key=lambda x: -x[1]):
            label = WF_LABELS.get(key, "—" if key == UNTAGGED else "?")
            disp = key if key != UNTAGGED else UNTAGGED
            L.append(f"| `{disp}` | {label} | {n} | {n / total * 100:.0f}% |")
    L.append("")
    failed = summary.get("failed_24h") or []
    L.append("### 直近 24h の FAILED セッション (#2024)")
    L.append("")
    if not failed:
        L.append("_なし (作成 24h 以内に FAILED で終わった session は無し)_")
    else:
        L.append("ℹ️ FAILED は **quota とは無関係**。quota 枯渇は session *作成*時に "
                 "`FAILED_PRECONDITION`/HTTP400 で弾かれる (作成自体が失敗)。ここに出るのは "
                 "**作成成功後に Jules 実行中に落ちた** もの = プラットフォーム側 transient が大半。"
                 "lock は stale-cleanup で解放され次 cron で再 pick されるため通常は自己修復する。"
                 "理由が空欄なら下記 id をダッシュボードで開き activity ログを確認。")
        L.append("")
        L.append("| id | age(h) | title | reason |")
        L.append("|---|---:|---|---|")
        for x in failed[:20]:
            reason = x.get("reason") or "_(理由フィールドなし→dashboard 参照)_"
            L.append(f"| `{x['id']}` | {x['age_h']:.1f} | {x['title']} | {reason} |")
    L.append("")
    stuck = summary["stuck"]
    L.append(f"### 放置 session (>{stuck_hours:g}h 人間待ち)")
    L.append("")
    if not stuck:
        L.append("_なし_")
    else:
        L.append("ℹ️ quota は食わない (concurrent 非占有 / daily は 24h で aging out) ので削除不要。"
                 "記事が生成されない content gap = #1600 の予防対象。")
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
                   help="この時間以上更新の無い AWAITING_* / PAUSED を放置扱い")
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
