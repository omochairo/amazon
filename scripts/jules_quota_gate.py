"""jules_quota_gate.py — Jules daily quota の *実 session 作成数* で create を絞る共有ゲート。

## なぜ必要か (真因 / session 114)
03-invoke-jules.yml の旧ガードは `google-labs-jules[bot]` が今日起票した **PR 数** を
DAILY_CAP=30 と比較していた。しかし Jules の daily quota (100, rolling 24h) を消費するのは
**session の作成** であり PR 生成ではない。session が PR に変換されない state
(PAUSED / FAILED / 遅い IN_PROGRESS / AWAITING_*) のまま積もると、PR=0 のまま
session 作成数だけ 100 に到達する。旧ガードは `PRs 0/30 = OK` と誤判定して発火せず、
さらに `created:$TODAY` (UTC 日付) 基準のため rolling-24h の実 quota とも境界がずれていた。
結果 02-publish→03-invoke 連鎖が 1 run=最大 12 session を作り続け 100/100 に張り付いた。

## 何をするか
Jules API (`GET /v1alpha/sessions`) を直接叩き、**直近 24h に作成された session 数**
(= 実 daily quota の消費量) を取得し、安全予算 (--cap, 既定 80) に対する残り枠を返す。
caller はこの値で 1 run の作成上限 (MAX_LOCKS 等) を動的に絞る。8 本の create 系
workflow が同じゲートを呼べば、全体で安全予算を超えない (= グローバルに rolling-24h を尊重)。

cap=80 は 100 から 20 の margin を残す設計: ゲート未導入の workflow がその 20 を使う。

## 使い方
    # この run で作ってよい session 数 (0..max) を stdout に整数で出す
    JULES_API_KEY=... python scripts/jules_quota_gate.py --cap 80 --max 12
    # → "12" (余裕時) / "3" (残り3) / "0" (枯渇) を 1 行で出力

caller 例 (bash):
    BUDGET=$(JULES_API_KEY=$KEY python scripts/jules_quota_gate.py --cap 80 --max 12)
    if [ "$BUDGET" -le 0 ]; then echo "quota gate: budget 0, skip"; exit 0; fi
    MAX_LOCKS=$BUDGET

## fail 挙動
- JULES_API_KEY 未設定 / API 取得失敗時は **--fallback (既定 1)** を返す (fail-slow)。
  完全 fail-closed (0) はパイプライン全停止を招き、fail-open (full) は本バグの再発を招く。
  間を取り「止めないが暴走もさせない」小さな予算を返す。fetch 失敗が複数 workflow で
  同時多発する日に fallback 分が合算で積み上がるため、2→1 に絞り累積を半減 (#2024 A)。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# createTime のパースは report_jules_sessions の pure helper を再利用 (rolling-24h
# 判定の定義を一箇所に保つ)。同モジュールの requests import は fetch_sessions 内の
# 遅延 import なので、_parse_ts の import 時点では requests 不要。
try:
    from report_jules_sessions import _parse_ts
except ImportError:  # スクリプト直叩き時の path 補完
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from report_jules_sessions import _parse_ts

# 03-invoke-jules.yml は setup-python / pip install を持たないため、ゲートは
# stdlib (urllib) のみで session を取得する (requests 非依存)。
API_BASE = "https://jules.googleapis.com/v1alpha/sessions"

# 2026-09-05: LIST /sessions が pageSize=100 / timeout=30s / retry 無しで恒常的に read
# timeout していた。ゲートは fallback 1 を返して緑で終わるため、03-invoke-jules の生成が
# 5 run × 6 → 5 run × 1 に落ちたまま気づかれなかった (08-24 断続 → 09-02 恒常。日次の
# 新規記事は 22〜43 本から 7 本へ)。requests 版 (report_jules_sessions) も同じ日に同じ
# read timeout を出しているので、HTTP クライアント差ではなく「1 ページの応答が 30s に
# 収まらない」側の問題。打ち手は 3 つ。運用中に振り直せるよう env で上書きできる:
#   - 1 リクエストの仕事量を減らす (pageSize 100 → 50)
#   - timeout を伸ばす (30s は実測に対して余裕が無い)
#   - 一過性の timeout / 5xx / 429 を指数バックオフで retry する (従来は 1 発勝負)
DEFAULT_PAGE_SIZE = int(os.environ.get("JULES_API_PAGE_SIZE", "50"))
DEFAULT_TIMEOUT = float(os.environ.get("JULES_API_TIMEOUT", "60"))
DEFAULT_ATTEMPTS = int(os.environ.get("JULES_API_ATTEMPTS", "3"))
DEFAULT_BACKOFF = float(os.environ.get("JULES_API_BACKOFF", "3"))


def _fetch_page(
    api_key: str,
    token: str,
    *,
    page_size: int,
    timeout: float,
    attempts: int,
    backoff: float,
    sleep=time.sleep,
) -> dict:
    """1 ページを取得する。一過性エラーは指数バックオフで retry し、尽きたら送出。

    retry を使い切ったときに partial な結果を返さないのは意図的。ページを取りこぼすと
    rolling-24h の作成数が過小になり、ゲートが予算を過大に返す = session 114 で踏んだ
    overshoot そのものを再現する。取れないときは呼び出し側で fallback に倒す。
    """
    params = {"pageSize": str(page_size)}
    if token:
        params["pageToken"] = token
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"X-Goog-Api-Key": api_key})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — timeout / 5xx / 429 を区別せず一律 retry
            last_exc = e
            if i + 1 < attempts:
                wait = backoff * (2 ** i)
                sys.stderr.write(
                    f"jules_quota_gate: page 取得失敗 ({e}) → {wait:.0f}s 後に retry "
                    f"({i + 2}/{attempts})\n"
                )
                sleep(wait)
    raise last_exc if last_exc is not None else RuntimeError("fetch failed")


def fetch_sessions(
    api_key: str,
    *,
    max_pages: int = 30,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    sleep=time.sleep,
) -> list[dict]:
    """LIST /v1alpha/sessions を pageToken で全ページ取得 (urllib, stdlib のみ)。"""
    sessions: list[dict] = []
    token = ""
    for _ in range(max_pages):
        body = _fetch_page(
            api_key,
            token,
            page_size=page_size,
            timeout=timeout,
            attempts=attempts,
            backoff=backoff,
            sleep=sleep,
        )
        sessions.extend(body.get("sessions") or [])
        token = body.get("nextPageToken") or ""
        if not token:
            break
    return sessions


def warn_step_summary(msg: str) -> None:
    """fallback に倒れたことを run summary に残す。

    stdout は BUDGET の値そのものとして `$(...)` に食われるので、`::warning::` の
    workflow command は出せない (出すと予算が壊れる)。代わりに GITHUB_STEP_SUMMARY へ
    書く。stderr だけだと job log を開くまで見えず、実際 08-24〜09-05 は誰も気づかな
    かった。
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n> ⚠️ **jules_quota_gate fallback**: {msg}\n")
    except OSError:  # summary が書けないことでゲートを落とさない
        pass


def count_created_24h(sessions: list[dict]) -> int:
    """createTime が直近 24h 以内の session 数 = 実 daily quota 消費量。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    n = 0
    for s in sessions:
        created = _parse_ts(s.get("createTime"))
        if created and (now - created).total_seconds() <= 86400:
            n += 1
    return n


def remaining_budget(created_24h: int, *, cap: int, per_run_max: int) -> int:
    """安全予算 cap に対する残り枠を 0..per_run_max にクランプして返す。"""
    return max(0, min(per_run_max, cap - created_24h))


def main() -> int:
    p = argparse.ArgumentParser(description="Jules rolling-24h session 作成数で create を絞る共有ゲート")
    p.add_argument("--cap", type=int, default=80,
                   help="安全 daily 予算 (Jules 上限 100 に対し margin を残す。既定 80)")
    p.add_argument("--max", dest="per_run_max", type=int, default=12,
                   help="1 run あたりの作成上限 (既定 12 = 旧 MAX_LOCKS)")
    p.add_argument("--fallback", type=int, default=1,
                   help="API 取得失敗 / キー未設定時に返す保守的予算 (既定 1)")
    args = p.parse_args()

    api_key = os.environ.get("JULES_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("jules_quota_gate: JULES_API_KEY 未設定 → fallback 予算を返す\n")
        warn_step_summary(
            f"JULES_API_KEY 未設定 → 予算 {args.fallback} (通常 {args.per_run_max})"
        )
        print(args.fallback)
        return 0
    try:
        sessions = fetch_sessions(api_key)
    except Exception as e:  # noqa: BLE001 — ゲート用途。失敗時は fail-slow で継続
        sys.stderr.write(f"jules_quota_gate: API 取得失敗 ({e}) → fallback 予算を返す\n")
        warn_step_summary(
            f"API 取得失敗 ({e}) → 予算 {args.fallback} (通常 {args.per_run_max})。"
            "この run の生成本数はほぼ 0 に落ちる"
        )
        print(args.fallback)
        return 0

    used = count_created_24h(sessions)
    budget = remaining_budget(used, cap=args.cap, per_run_max=args.per_run_max)
    sys.stderr.write(
        f"jules_quota_gate: rolling-24h 作成 {used} / cap {args.cap} "
        f"→ この run の予算 {budget} (per-run max {args.per_run_max})\n"
    )
    print(budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
