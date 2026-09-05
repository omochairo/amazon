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
# 上の 3 つは 1 ページあたりの粘りしか決めない。全体の上限が無いと
# 30 ページ × 3 回 × 60s = 最悪 90 分ゲートで止まる。実際 #6501 直後の
# 12-rewrite-idle-fill (2026-09-05 07:24 dispatch) が該当ステップで 10 分以上
# 返らなくなった。**fallback へ倒れるまでの時間**を wall-clock で切る。
# 180s は「retry 1〜2 回ぶんは粘るが、run を止めない」ところ。旧実装 (30s 1 発) より
# 遅く、cron 間隔 (6h) に対しては十分短い。
# 実測 (2026-09-05 07:24 の 12-rewrite-idle-fill): 全ページ走査に **約 14 分**
# 掛かって成功した (fallback ではなく実値 0 が返った)。timeout=30s ではこの遅さが
# そのまま失敗になっていた。deadline はこの実測を殺さない値に置く。早期打ち切り
# (下記) が効けば通常はここまで掛からない。
DEFAULT_DEADLINE = float(os.environ.get("JULES_API_DEADLINE", "900"))
# 走査する session の総数。**pageSize を下げたぶん page 数を上げて、旧実装
# (100 × 30 = 3,000) と同じ被覆を保つ。** ここを詰めると、API が新しい順に返して
# いなかった場合に rolling-24h の作成数を取りこぼし、予算が過大になる
# (= session 114 の overshoot)。並び順は未確認なので被覆は減らさない。
DEFAULT_MAX_SESSIONS = int(os.environ.get("JULES_API_MAX_SESSIONS", "3000"))


class DeadlineExceeded(Exception):
    """全体の制限時間を使い切った。呼び出し側は fallback に倒す。"""


def _fetch_page(
    api_key: str,
    token: str,
    *,
    page_size: int,
    timeout: float,
    attempts: int,
    backoff: float,
    remaining: float | None = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    """1 ページを取得する。一過性エラーは指数バックオフで retry し、尽きたら送出。

    retry を使い切ったときに partial な結果を返さないのは意図的。ページを取りこぼすと
    rolling-24h の作成数が過小になり、ゲートが予算を過大に返す = session 114 で踏んだ
    overshoot そのものを再現する。取れないときは呼び出し側で fallback に倒す。

    ``remaining`` は残りの持ち時間 (秒)。socket timeout はこれを超えないよう縮め、
    バックオフの待機も残り時間を食い潰さない範囲でしか入れない。
    """
    params = {"pageSize": str(page_size)}
    if token:
        params["pageToken"] = token
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    attempts = max(1, attempts)
    started = monotonic()

    def _left() -> float | None:
        if remaining is None:
            return None
        return remaining - (monotonic() - started)

    last_exc: Exception | None = None
    for i in range(attempts):
        left = _left()
        if left is not None and left <= 0:
            raise DeadlineExceeded("制限時間内に取得できなかった") from last_exc
        eff_timeout = timeout if left is None else min(timeout, left)
        try:
            req = urllib.request.Request(url, headers={"X-Goog-Api-Key": api_key})
            with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — timeout / 5xx / 429 を区別せず一律 retry
            last_exc = e
            if i + 1 >= attempts:
                break
            wait = backoff * (2 ** i)
            left = _left()
            if left is not None and left - wait <= 0:
                # 待つと持ち時間が尽きる = retry しても投げるだけ。ここで打ち切る。
                raise DeadlineExceeded("制限時間内に取得できなかった") from e
            sys.stderr.write(
                f"jules_quota_gate: page 取得失敗 ({e}) → {wait:.0f}s 後に retry "
                f"({i + 2}/{attempts})\n"
            )
            sleep(wait)
    raise last_exc if last_exc is not None else RuntimeError("fetch failed")


def fetch_sessions(
    api_key: str,
    *,
    max_pages: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    deadline: float | None = DEFAULT_DEADLINE,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> list[dict]:
    """LIST /v1alpha/sessions を pageToken で全ページ取得 (urllib, stdlib のみ)。

    ``deadline`` は全ページ合計の持ち時間 (秒)。超えたら ``DeadlineExceeded`` を投げ、
    呼び出し側が fallback 予算に倒す。ページ単位の retry しか無いと
    ページ数 × attempts × timeout ぶん待ちうるので、外側にも上限を置く。

    ``max_pages`` 未指定時は ``DEFAULT_MAX_SESSIONS`` / ``page_size`` から決める
    (pageSize を下げても走査できる session 総数を変えないため)。
    """
    if max_pages is None:
        # 被覆 (page_size × max_pages) を DEFAULT_MAX_SESSIONS 以上に保つ。
        max_pages = -(-DEFAULT_MAX_SESSIONS // max(1, page_size))
    sessions: list[dict] = []
    token = ""
    started = monotonic()
    for _ in range(max_pages):
        remaining = None
        if deadline is not None:
            remaining = deadline - (monotonic() - started)
            if remaining <= 0:
                raise DeadlineExceeded(
                    f"{deadline:.0f}s 以内に全ページを取得できなかった "
                    f"({len(sessions)} 件取得済みだが partial は使わない)"
                )
        body = _fetch_page(
            api_key,
            token,
            page_size=page_size,
            timeout=timeout,
            attempts=attempts,
            backoff=backoff,
            remaining=remaining,
            sleep=sleep,
            monotonic=monotonic,
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


def _sorted_desc(sessions: list[dict]) -> bool:
    """createTime が新しい順に並んでいるか (欠損は判定から外す)。"""
    seen = [_parse_ts(s.get("createTime")) for s in sessions]
    seen = [t for t in seen if t]
    return all(a >= b for a, b in zip(seen, seen[1:]))


def count_created_24h_early_stop(
    api_key: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    deadline: float | None = DEFAULT_DEADLINE,
    max_pages: int | None = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> int:
    """rolling-24h の作成数を、必要なページだけ読んで数える。

    ## なぜ要るか

    ゲートが欲しいのは「直近 24h に作成された session 数」だけなのに、旧実装は
    **全ページを舐めてから** 24h で絞っていた。2026-09-05 の実測で全走査は約 14 分。
    session は API に DELETE が無く増える一方なので、この時間は伸び続ける。

    ## 並び順を仮定しない

    「新しい順に返る」と決め打ちして打ち切ると、実際が別の順序だったときに
    取りこぼして予算が過大になる (= session 114 の overshoot)。そこで**読んだ範囲が
    実際に新しい順になっていることを確認できたときだけ**打ち切る:

    - ここまでに読んだ session が createTime の降順になっている、かつ
    - 直近ページの最後が 24h より古い

    の両方が成り立った時点で以降のページには 24h 以内が無いと言える。並びが降順で
    なければ確認が成立しないので、従来どおり全ページ読む (安全側)。
    """
    if max_pages is None:
        max_pages = -(-DEFAULT_MAX_SESSIONS // max(1, page_size))
    sessions: list[dict] = []
    token = ""
    started = monotonic()
    for _ in range(max_pages):
        remaining = None
        if deadline is not None:
            remaining = deadline - (monotonic() - started)
            if remaining <= 0:
                raise DeadlineExceeded(
                    f"{deadline:.0f}s 以内に取得できなかった (partial は使わない)"
                )
        body = _fetch_page(
            api_key, token,
            page_size=page_size, timeout=timeout, attempts=attempts,
            backoff=backoff, remaining=remaining, sleep=sleep, monotonic=monotonic,
        )
        page = body.get("sessions") or []
        sessions.extend(page)
        token = body.get("nextPageToken") or ""
        if not token:
            break
        last = _parse_ts(page[-1].get("createTime")) if page else None
        if last and _sorted_desc(sessions):
            age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
            if age > 86400:
                sys.stderr.write(
                    f"jules_quota_gate: {len(sessions)} 件で 24h 境界に到達 "
                    f"(降順を確認済み) → 以降のページは読まない\n"
                )
                break
    return count_created_24h(sessions)


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
        used = count_created_24h_early_stop(api_key)
    except Exception as e:  # noqa: BLE001 — ゲート用途。失敗時は fail-slow で継続
        sys.stderr.write(f"jules_quota_gate: API 取得失敗 ({e}) → fallback 予算を返す\n")
        warn_step_summary(
            f"API 取得失敗 ({e}) → 予算 {args.fallback} (通常 {args.per_run_max})。"
            "この run の生成本数はほぼ 0 に落ちる"
        )
        print(args.fallback)
        return 0

    budget = remaining_budget(used, cap=args.cap, per_run_max=args.per_run_max)
    sys.stderr.write(
        f"jules_quota_gate: rolling-24h 作成 {used} / cap {args.cap} "
        f"→ この run の予算 {budget} (per-run max {args.per_run_max})\n"
    )
    print(budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
