"""open_stale_pr_issue.py

#4469: **open のまま滞留している PR** を 1 本の Issue に集約して起票/更新する sweeper。

なぜ必要か (#4280 の実測):
  05-jules-auto-merge.yml のスコープガードに引っかかった記事 PR が、check は pass
  表示・`mergeable_state: clean` のまま 4 日間 open で残り、記事 1 本の公開が遅れた。
  skip は run ログには出ていたので、これは logging の失敗ではなく **monitoring の失敗**
  だった。よって「skip を可視化する」だけでは同じ結果になる (誰も run ログを見ない)。
  この sweeper は原因非依存で「動いていない PR」を拾うので、スコープ外 skip だけでなく
  validate 赤・Jules 途中終了・lock 詰まり・auto-merge 未設定も同じ網に入る。

設計判断:
- **起票は常に 1 件**。マーカー `<!-- stale-pr-sweeper -->` で open を特定し、あれば body を
  更新する (CLAUDE.md の GitHub API バースト禁止 = 一括起票しない)。
- **滞留ゼロになったら close する**。open_quarantine_issue.py 等の姉妹スクリプトは
  「close は人間が判断」だが、あちらが人間がキュレーションする worklist なのに対し、
  こちらは一時状態のミラーでしかない。PR がマージされた後も「#X が止まっています」と
  書かれた Issue が open で残ると、TODO 一次管理面 (`gh issue list`) のノイズになる。
- draft PR は除外する。作業中の PR を毎日名指しされないための逃げ道でもある。
- しきい値は既定 24h。記事 PR は通常 1 分以内、data レーンの PR も同日中にマージされる
  ので、24h 残っている時点で自動フローから外れている。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Optional

try:  # package 実行 (`python -m scripts.x`) と素実行の両対応
    from scripts._marked_issue import find_marked_issue
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts._marked_issue import find_marked_issue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_stale_pr_issue")

MARKER = "stale-pr-sweeper"
LABELS = "tech-debt,todo"
DEFAULT_THRESHOLD_HOURS = 24

PR_FIELDS = (
    "number,title,url,createdAt,updatedAt,isDraft,author,labels,"
    "mergeStateStatus,headRefName"
)


def _parse_ts(value: str) -> dt.datetime:
    """GitHub の ISO 8601 (`2026-08-04T01:23:45Z`) を aware datetime にする。"""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_open_prs(repo: str, limit: int = 100) -> List[Dict[str, Any]]:
    res = subprocess.run(
        ["gh", "pr", "list", "-R", repo, "--state", "open",
         "--limit", str(limit), "--json", PR_FIELDS],
        check=True, capture_output=True, text=True,
    )
    return json.loads(res.stdout or "[]")


def select_stale(
    prs: List[Dict[str, Any]], now: dt.datetime, threshold_hours: int
) -> List[Dict[str, Any]]:
    """draft を除いた open PR のうち、作成から threshold_hours 以上経ったものを古い順に返す。

    `updatedAt` ではなく `createdAt` で測る。滞留 PR には bot が push や
    ラベル付けをして updatedAt だけ新しくなるケースがあり、それを「動いている」と
    誤認すると #4280 (4 日間放置) をそのまま見逃す。
    """
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        created = _parse_ts(pr["createdAt"])
        age_h = (now - created).total_seconds() / 3600.0
        if age_h < threshold_hours:
            continue
        enriched = dict(pr)
        enriched["age_hours"] = age_h
        out.append(enriched)
    return sorted(out, key=lambda p: -p["age_hours"])


def _fmt_age(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.1f} 日"
    return f"{hours:.0f} 時間"


def render_body(rows: List[Dict[str, Any]], threshold_hours: int) -> str:
    parts = [
        f"<!-- {MARKER} -->",
        "元: #4469 (auto-merge スコープ外の article PR が無言で放置される)",
        "",
        "## 概要",
        "",
        f"作成から **{threshold_hours}h 以上** open のままの PR (draft を除く) が "
        f"**{len(rows)} 件** あります。自動マージのフローから外れている可能性が高いので、"
        "マージするか close するか判断してください。",
        "",
        "| PR | 経過 | merge state | ラベル | 作成者 |",
        "|---|---|---|---|---|",
    ]
    for pr in rows:
        title = str(pr.get("title", "")).replace("|", "／")
        labels = ", ".join(l.get("name", "") for l in (pr.get("labels") or [])) or "—"
        author = (pr.get("author") or {}).get("login", "?")
        parts.append(
            f"| [#{pr['number']}]({pr['url']}) {title} | {_fmt_age(pr['age_hours'])} "
            f"| {pr.get('mergeStateStatus', '?')} | {labels} | {author} |"
        )
    parts.extend([
        "",
        "## よくある原因",
        "",
        "- `auto-merge-skipped` ラベル付き → 05-jules-auto-merge のスコープ外 "
        "(記事以外のファイルが混入)。中身を確認して手動マージする。",
        "- `mergeStateStatus` が `BLOCKED` / `DIRTY` → required check 失敗か conflict。",
        "- `CLEAN` なのに残っている → auto-merge がそもそも有効化されていない。",
        "- `UNKNOWN` → GitHub 側が mergeability を未計算なだけ。PR を開けば確定する。",
        "",
        "## 自動運用",
        "",
        f"- マーカー `<!-- {MARKER} -->` で同一 open Issue を特定し、body を毎回更新します"
        " (新規起票は増やしません)。",
        "- 滞留 PR がゼロになると、この Issue は次回 run で自動 close されます。",
        "- 作業中の PR を除外したいときは draft にしてください。",
    ])
    return "\n".join(parts)


def get_open_issue(repo: str) -> Optional[int]:
    """マーカーを含む open Issue の番号を返す (無ければ None)。"""
    query = f'repo:{repo} is:issue is:open in:body "{MARKER}"'
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=10"],
        check=True, capture_output=True, text=True,
    )
    items = json.loads(res.stdout).get("items", [])
    # 検索は絞り込みでしかない。本文のマーカーで裏を取る (_marked_issue の docstring)
    found = find_marked_issue(items, MARKER)
    return found["number"] if found else None


def create_issue(repo: str, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "create", "-R", repo,
         "--title", title, "--label", LABELS, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "edit", str(number), "-R", repo,
         "--title", title, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def close_issue(repo: str, number: int) -> None:
    subprocess.run(
        ["gh", "issue", "close", str(number), "-R", repo,
         "--comment", "滞留 PR がゼロになったため自動 close します (sweeper)。"],
        check=True, capture_output=True, text=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--threshold-hours", type=int,
                   default=int(os.environ.get("THRESHOLD_HOURS") or DEFAULT_THRESHOLD_HOURS))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    prs = list_open_prs(args.repo)
    rows = select_stale(prs, dt.datetime.now(dt.timezone.utc), args.threshold_hours)
    logger.info("open PRs=%d, stale (>=%dh, non-draft)=%d",
                len(prs), args.threshold_hours, len(rows))
    for pr in rows:
        logger.info("  stale #%s %s (%s)", pr["number"],
                    _fmt_age(pr["age_hours"]), pr.get("mergeStateStatus"))

    if args.dry_run:
        body = render_body(rows, args.threshold_hours) if rows else "(no stale PRs)"
        out = pathlib.Path("_stale_pr_issue_preview.md")
        out.write_text(body, encoding="utf-8")
        logger.info("dry-run: body -> %s", out)
        return 0

    number = get_open_issue(args.repo)

    if not rows:
        if number is not None:
            close_issue(args.repo, number)
            logger.info("closed #%s (no stale PRs)", number)
        else:
            logger.info("no stale PRs and no open sweeper issue — nothing to do")
        return 0

    title = f"[sweeper] {args.threshold_hours}h 以上 open のままの PR {len(rows)} 件"
    body = render_body(rows, args.threshold_hours)
    if number is not None:
        logger.info("updated #%s: %s", number,
                    update_issue(args.repo, number, title, body))
    else:
        logger.info("created: %s", create_issue(args.repo, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
