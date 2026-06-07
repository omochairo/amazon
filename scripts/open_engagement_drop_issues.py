"""open_engagement_drop_issues.py

A-4 (epic #1356): scripts/detect_engagement_drop.py の出力
(`data/analytics/engagement_drop.json`) を読み、engagement が落ちている各 URL に
ついて Jules リライト提案 Issue を per-URL で起票する。

重複防止マーカー: `<!-- a4-engagement-drop:<URL> -->`
ラベル: quality,todo,analytics

A-1 (CTR) / A-2 (position) と分離する理由:
- 入力が GSC ではなく GA4 (流入後の読者行動シグナル)
- アクションが「SERP 改善」ではなく「本文・冒頭の読み応え改善」
- marker prefix が異なるので独立 dedup される
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_engagement_drop_issues")

DEFAULT_IN = "data/analytics/engagement_drop.json"
MARKER_PREFIX = "a4-engagement-drop:"
LABELS = "quality,todo,analytics"


def find_existing_taken(repo: str) -> set[str]:
    query = (
        f"repo:{repo} is:issue is:open label:quality label:analytics "
        f'in:body "{MARKER_PREFIX}"'
    )
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=100"],
        check=True, capture_output=True, text=True,
    )
    items = json.loads(res.stdout).get("items", [])
    taken: set[str] = set()
    for it in items:
        body = it.get("body") or ""
        idx = body.find(MARKER_PREFIX)
        while idx >= 0:
            tail = body[idx + len(MARKER_PREFIX):]
            url = tail.split("-->", 1)[0].strip()
            if url:
                taken.add(url)
            idx = body.find(MARKER_PREFIX, idx + 1)
    return taken


def render_body(d: dict, *, src_range: dict) -> str:
    url = d["url"]
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    parts = [
        f"<!-- {MARKER_PREFIX}{url} -->",
        "親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## エンゲージメント低下概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{url}` |",
        f"| 観察期間 | {rng} |",
        f"| PV | {d['screen_page_views']} |",
        f"| engagement rate | {d['engagement_rate']*100:.1f}% |",
        f"| 平均滞在時間 | {d['avg_session_duration']:.1f} 秒 |",
        f"| engaged sessions | {d['engaged_sessions']} |",
        f"| bounce rate | {d['bounce_rate']*100:.1f}% |",
        "",
        "## アクション提案",
        "",
        f"このページは PV {d['screen_page_views']} を獲得しているが、engagement rate "
        f"{d['engagement_rate']*100:.1f}% / 平均滞在 {d['avg_session_duration']:.1f}s と、"
        "流入後ほぼ読まれず即離脱している。検索意図と本文のズレ、または冒頭で価値が"
        "伝わっていない可能性が高い。Jules リライト対象。",
        "",
        "1. **冒頭フックの見直し** — 1 スクロール目で「この記事で何が分かるか」を結論先出し",
        "2. **検索意図との整合** — 流入クエリ (GSC 側で確認) が期待する答えを above-the-fold に",
        "3. **内容の厚み** — 比較表 / 体験談 / FAQ など滞在を伸ばす要素を追加",
        "4. **内部リンク** — 次に読みたくなる関連記事を本文中 + 末尾に設置し回遊を促す",
        "5. **ページ表示速度** — 画像最適化等で初期表示を軽く (即離脱の物理要因を除去)",
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{url} -->` マーカーを埋め込み済み",
        "- 同 URL が再検出されてもこの Issue が open なら新規起票なし",
    ]
    return "\n".join(parts)


def create_issue(repo: str, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "create", "-R", repo,
         "--title", title, "--label", LABELS, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2

    data = json.loads(in_path.read_text(encoding="utf-8"))
    detected = data.get("detected") or []
    src_range = data.get("source_range") or {}

    if not detected:
        logger.info("no engagement-drop pages — exiting")
        return 0

    taken = find_existing_taken(args.repo)
    logger.info("existing engagement-drop open Issues: %d", len(taken))

    created = 0
    for d in detected:
        url = d["url"]
        if url in taken:
            logger.info("skip (already open): %s", url)
            continue
        title = (f"[A-4] Engagement drop: {url} "
                 f"(PV={d['screen_page_views']}, eng={d['engagement_rate']*100:.0f}%, "
                 f"{d['avg_session_duration']:.0f}s)")
        body = render_body(d, src_range=src_range)
        if args.dry_run:
            logger.info("would create: %s", title)
            continue
        u = create_issue(args.repo, title, body)
        logger.info("created: %s", u)
        created += 1

    logger.info("created %d new Issue(s)", created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
