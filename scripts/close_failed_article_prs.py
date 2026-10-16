"""close_failed_article_prs.py

validate (品質ゲート) で落ちたまま止まっている **Jules の記事 PR** を close する。

なぜ必要か (2026-10-16 の実測):
  navi-brain#89 以降の Jules は、品質ゲートで落ちても記事を削らずに validate 赤のまま
  セッションを終える。赤い記事 PR を閉じる経路がどこにも無く、10-09 以降に人が閉じるのを
  やめた結果、10-13〜10-16 の 4 日で 18 本が open のまま積み上がった (17 本が sources_v5、
  1 本が howto_title_promise)。

  一方で、03-invoke-jules の pick (article_pick.exclude_recently_failed, #9239) は
  「マージされずに close された記事 PR」を前提に組んである:
    - close から 14 日はその ASIN を選ばない (素材を取り直したら即戻す)
    - 30 日で 2 回 close された ASIN は人に回す
  open のままだと、この仕組みが働かないうえ、ASIN は open PR として除外され続けるので
  書き直しの候補にも戻らない。ここで close して、既存の待ち・打ち切りに乗せる。

対象 (全部満たすものだけ。人の PR を巻き込まないよう狭く取る):
- draft でない・`keep-open` ラベルが無い
- 変更ファイルが記事 JSON (data/articles/YYYY-MM-DD-B0XXXXXXXX.json) を 1 件以上含み、
  残りも 05-jules-auto-merge のスコープ (data/articles/ / data/raw/per_asin/ /
  hugo/content/brands/*/_index.md) に収まる
- 最新の `validate` チェックが FAILURE で、その完了から grace 時間 (既定 6h) 以上経っている
  (Jules や 05 の自動修復が数十分後に push し直すことがあるので、すぐには閉じない)

GitHub API バースト禁止 (CLAUDE.md): 1 run で close するのは最大 max_close 件 (既定 10)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("close_failed_article_prs")

DEFAULT_GRACE_HOURS = 6
DEFAULT_MAX_CLOSE = 10
KEEP_LABEL = "keep-open"
CHECK_NAME = "validate"

PR_FIELDS = "number,title,isDraft,labels,files,statusCheckRollup,headRefName"

ARTICLE_RE = re.compile(r"^data/articles/\d{4}-\d{2}-\d{2}-B0[A-Z0-9]{8}\.json$")
# 05-jules-auto-merge.yml の scope ステップと同じ範囲
SCOPE_RE = re.compile(r"^(data/articles/|data/raw/per_asin/|hugo/content/brands/[^/]+/_index\.md$)")

COMMENT = (
    "品質ゲート (validate) で不合格のまま {hours} 時間以上経過したため、自動で close します。\n\n"
    "この ASIN は 03-invoke-jules の pick で 14 日間選ばれません (事前収集の素材を取り直せば"
    "その時点で候補に戻ります)。30 日で 2 回 close されると自動では選ばれなくなり、"
    "人の判断待ちになります (#9239)。\n\n"
    "残したい PR には `keep-open` ラベルを付けてください。"
)


def _parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def is_article_pr(pr: Dict[str, Any]) -> bool:
    """記事 PR (Jules の自動生成と同じ形) か。"""
    paths = [f.get("path", "") for f in (pr.get("files") or []) if isinstance(f, dict)]
    if not paths:
        return False
    if not any(ARTICLE_RE.match(p) for p in paths):
        return False
    return all(SCOPE_RE.match(p) for p in paths)


def latest_check(pr: Dict[str, Any], name: str = CHECK_NAME) -> Optional[Dict[str, Any]]:
    """statusCheckRollup から name の最新 (完了時刻が一番新しい) チェックを返す。"""
    runs = [c for c in (pr.get("statusCheckRollup") or [])
            if isinstance(c, dict) and c.get("name") == name]
    if not runs:
        return None
    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return max(runs, key=lambda c: _parse_ts(c.get("completedAt")) or epoch)


def select_failed(prs: List[Dict[str, Any]], now: dt.datetime,
                  grace_hours: float) -> List[Dict[str, Any]]:
    """close 対象の PR を、validate の完了が古い順に返す。"""
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        if any(l.get("name") == KEEP_LABEL for l in (pr.get("labels") or [])):
            continue
        if not is_article_pr(pr):
            continue
        check = latest_check(pr)
        if not check or check.get("status") not in (None, "COMPLETED"):
            continue
        if check.get("conclusion") != "FAILURE":
            continue
        done = _parse_ts(check.get("completedAt"))
        if done is None:
            continue
        age_h = (now - done).total_seconds() / 3600.0
        if age_h < grace_hours:
            continue
        out.append({"number": pr["number"], "title": pr.get("title", ""), "age_hours": age_h})
    return sorted(out, key=lambda p: -p["age_hours"])


def list_open_prs(repo: str, limit: int = 100) -> List[Dict[str, Any]]:
    res = subprocess.run(
        ["gh", "pr", "list", "-R", repo, "--state", "open",
         "--limit", str(limit), "--json", PR_FIELDS],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(res.stdout or "[]")


def close_pr(repo: str, number: int, grace_hours: float) -> bool:
    res = subprocess.run(
        ["gh", "pr", "close", str(number), "-R", repo, "--delete-branch",
         "--comment", COMMENT.format(hours=int(grace_hours))],
        capture_output=True, text=True, encoding="utf-8",
    )
    if res.returncode != 0:
        logger.warning("close #%s failed: %s", number, (res.stderr or "").strip())
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("REPO", "omochairo/amazon"))
    ap.add_argument("--grace-hours", type=float,
                    default=float(os.environ.get("GRACE_HOURS") or DEFAULT_GRACE_HOURS))
    ap.add_argument("--max-close", type=int,
                    default=int(os.environ.get("MAX_CLOSE") or DEFAULT_MAX_CLOSE))
    ap.add_argument("--dry-run", action="store_true",
                    default=os.environ.get("DRY_RUN", "").lower() == "true")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    targets = select_failed(list_open_prs(args.repo), now, args.grace_hours)
    logger.info("validate 赤のまま %.0fh 以上の記事 PR: %d 件", args.grace_hours, len(targets))
    closed = 0
    for t in targets[: args.max_close]:
        logger.info("#%s (%.1fh) %s", t["number"], t["age_hours"], t["title"])
        if args.dry_run:
            continue
        if close_pr(args.repo, t["number"], args.grace_hours):
            closed += 1
        time.sleep(2)  # API を連打しない
    if len(targets) > args.max_close:
        logger.info("残り %d 件は次回 run で close する", len(targets) - args.max_close)
    logger.info("closed=%d dry_run=%s", closed, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
