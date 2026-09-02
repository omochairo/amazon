"""close_expired_analytics_issues.py

A レーン検出器 (A-1〜A-5 / A-7, epic #1356) が起票した per-URL Issue のうち、
本文の `<!-- analytics-expires:YYYY-MM-DD -->` マーカーが示す期限を過ぎたものを
close する。

## なぜ必要か

A レーンの Issue は「ある観測週のスナップショット」であって恒久課題ではない。
放置すると 2 つ壊れる:

1. 古い数値の Issue が現況の候補に混ざり、棚卸しが人手作業になる
   (2026-09-02 に amazon-navi-brain で 3 件を手で閉じた)
2. **各 opener の marker guard が新しい観測の起票をブロックする** — dedup は
   「同 URL の open な Issue があれば skip」なので、古い 1 件が残っていると
   その URL は二度と最新の数値で立ち直らない。こちらのほうが実害が大きい

そのため本スクリプトは 17-analytics-report.yml の **opener 群より前**に走る。
期限切れを先に閉じてから検出結果を起票することで、まだ検出され続けている URL は
同じ run の中で最新の数値の Issue に置き換わる。

## 安全側の設計

- マーカーが無い Issue は**触らない**。人が立てた Issue や、観測期間を読めなかった
  自動 Issue を巻き込まないため
- 期限「当日」は閉じない (`today > expiry` のときだけ)
- `--max-close` (既定 30) で 1 run の close 数に上限を置く。GitHub API の
  バースト規律 (.claude/CLAUDE.md) に従う。上限に当たったら警告して残りは次 run へ
- close 理由は `not planned`。「対応した」ではなく「期限切れで現況を表さなくなった」
  ので completed にはしない

使い方:

    python -m scripts.close_expired_analytics_issues --repo owner/name [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys

from scripts._analytics_closed_keys import extract_dedup_keys, write_closed_keys
from scripts._analytics_issue_expiry import MARKER_PREFIX, find_expiry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("close_expired_analytics_issues")

DEFAULT_MAX_CLOSE = 30
CLOSE_COMMENT = (
    "有効期限 ({expiry}) を過ぎたため自動 close しました。\n\n"
    "この Issue は特定の観測週のスナップショットで、記載の数値はもう現況を表しません。"
    "同じ URL がまだ検出され続けているなら、次の週次 run が最新の数値で立て直します "
    "(本 Issue が open のままだと重複防止マーカーがその起票をブロックしてしまうため、"
    "期限を切っています)。\n\n"
    "恒久的に追うべき課題だと判断した場合は、この Issue を reopen せず、"
    "期限マーカーを持たない別 Issue に切り出してください。"
)


def search_marked_issues(repo: str) -> list[dict]:
    """期限マーカーを持つ open Issue を集める (最大 100 件 = 1 ページ)。"""
    query = f'repo:{repo} is:issue is:open in:body "{MARKER_PREFIX}"'
    # 1 ページ (100 件) しか見ない。マーカー付きの open が 100 を超えると溢れるが、
    # 作成の古い順に並べているので**溢れるのは必ず新しい側** = まだ期限内のもの。
    # 期限切れを取りこぼさない側に倒している。
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=100",
         "-f", "sort=created", "-f", "order=asc"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(res.stdout).get("items", [])


def close_issue(repo: str, number: int, expiry: dt.date) -> None:
    subprocess.run(
        ["gh", "issue", "close", str(number), "-R", repo,
         "-r", "not planned",
         "-c", CLOSE_COMMENT.format(expiry=expiry.isoformat())],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def select_expired(items: list[dict], *, today: dt.date) -> list[dict]:
    """期限切れの Issue を期限の古い順に返す。

    `--max-close` で切り捨てるときに古いものから消化されるよう、ここで並べておく。
    """
    out: list[dict] = []
    for it in items:
        expiry = find_expiry(it.get("body"))
        if expiry is None:
            continue
        if today <= expiry:
            continue
        out.append({
            "number": int(it["number"]),
            "expiry": expiry,
            "title": it.get("title") or "",
            "body": it.get("body") or "",
        })
    out.sort(key=lambda d: d["expiry"])
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--max-close", type=int, default=DEFAULT_MAX_CLOSE)
    p.add_argument("--today", help="YYYY-MM-DD (テスト用。既定は UTC の今日)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(dt.timezone.utc).date())

    items = search_marked_issues(args.repo)
    logger.info("open Issues with expiry marker: %d", len(items))

    expired = select_expired(items, today=today)
    if not expired:
        logger.info("no expired Issues as of %s", today.isoformat())
        # 何も閉じなくても空で上書きする。runner が使い回される環境で前 run の
        # 残骸が残ると、opener が「今 close された」と誤認して重複起票しうる。
        if not args.dry_run:
            write_closed_keys({})
        return 0

    if len(expired) > args.max_close:
        logger.warning(
            "expired=%d exceeds --max-close=%d — closing the oldest %d, "
            "the rest will be picked up by the next run",
            len(expired), args.max_close, args.max_close,
        )
        expired = expired[:args.max_close]

    closed = 0
    closed_keys: dict[str, list[str]] = {}
    for item in expired:
        number, expiry = item["number"], item["expiry"]
        if args.dry_run:
            logger.info("would close #%d (expired %s): %s",
                        number, expiry.isoformat(), item["title"])
            continue
        close_issue(args.repo, number, expiry)
        logger.info("closed #%d (expired %s): %s", number, expiry.isoformat(), item["title"])
        closed += 1
        # 閉じた Issue が押さえていた重複防止キーを opener へ渡す。search 索引が
        # まだ open を返しても、opener がこれを差し引いて立て直せるようにするため。
        for prefix, keys in extract_dedup_keys(item["body"]).items():
            bucket = closed_keys.setdefault(prefix, [])
            bucket.extend(k for k in keys if k not in bucket)

    logger.info("closed %d expired Issue(s)", closed)
    if not args.dry_run:
        write_closed_keys(closed_keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
