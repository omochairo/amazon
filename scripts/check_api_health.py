"""``_api_health.record`` の記録を集計し、壊れている外部 API があれば exit 1 (#8272)。

判定 (API ごと):
- 恒常的な失敗 (429 以外の 4xx) が呼び出しの半数以上 → 廃止・設定不備・認証切れ
- 3 回以上呼んで 1 回も 200 が無い → 全面停止
429 / 5xx / 無応答は一時的なものとして、前者の判定には数えない。楽天 Stage2 の
「keyword 不正」の 400 のように正常系で出る 4xx もあるので、閾値は割合で見る。

データ PR の作成より後の step で呼ぶこと。ここで落ちてもデータ更新は止まらず、
run だけが赤くなる。

使い方:
    python scripts/check_api_health.py [--log PATH]   # 省略時は $API_HEALTH_LOG
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _api_health import ENV_VAR  # noqa: E402

PERSISTENT_FAIL_RATIO = 0.5
OUTAGE_MIN_CALLS = 3


# 404 が「商品ページが消えた」という正常な答えになる API (#12305 refresh_shop_prices.py)。
# 確かめ直しは古い順に回すので、消えた商品が多い日もある。これを故障と数えない
# トイザらスの店に無い商品も 404 で返る (fetch_store_offers.py)
NOT_FOUND_IS_ANSWER = frozenset({"rakuten_ichiba_refresh", "rakuten_books_refresh", "rakuten_store_offer"})


def _is_answer(api: str, status: int | None) -> bool:
    return status == 200 or (status == 404 and api in NOT_FOUND_IS_ANSWER)


def _is_persistent_failure(status: int | None, api: str = "") -> bool:
    if status == 404 and api in NOT_FOUND_IS_ANSWER:
        return False
    return status is not None and 400 <= status < 500 and status != 429


def load(path: str) -> dict[str, list[int | None]]:
    by_api: dict[str, list[int | None]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("api"):
                by_api[row["api"]].append(row.get("status"))
    return dict(by_api)


def evaluate(by_api: dict[str, list[int | None]]) -> list[dict]:
    rows = []
    for api in sorted(by_api):
        statuses = by_api[api]
        total = len(statuses)
        ok = sum(1 for s in statuses if _is_answer(api, s))
        persistent = sum(1 for s in statuses if _is_persistent_failure(s, api))
        reason = ""
        if persistent / total >= PERSISTENT_FAIL_RATIO:
            reason = f"429 以外の 4xx が {persistent}/{total}"
        elif ok == 0 and total >= OUTAGE_MIN_CALLS:
            reason = f"{total} 回呼んで成功 0"
        codes = sorted({str(s) for s in statuses if s != 200})
        rows.append({
            "api": api, "total": total, "ok": ok, "persistent": persistent,
            "codes": codes, "reason": reason,
        })
    return rows


def render_summary(rows: list[dict]) -> str:
    lines = ["### 外部 API ヘルスチェック", "",
             "| API | 呼び出し | 成功 | 429以外の4xx | 失敗コード | 判定 |",
             "|---|---:|---:|---:|---|---|"]
    for r in rows:
        verdict = f"NG: {r['reason']}" if r["reason"] else "OK"
        lines.append(f"| {r['api']} | {r['total']} | {r['ok']} | {r['persistent']} "
                     f"| {', '.join(r['codes']) or '-'} | {verdict} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.environ.get(ENV_VAR))
    args = ap.parse_args()

    if not args.log or not os.path.exists(args.log):
        print(f"no API health log ({args.log!r}) — nothing recorded")
        return 0

    rows = evaluate(load(args.log))
    summary = render_summary(rows)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)

    bad = [r for r in rows if r["reason"]]
    for r in bad:
        print(f"::error::external API unhealthy: {r['api']} ({r['reason']}, codes={r['codes']})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
