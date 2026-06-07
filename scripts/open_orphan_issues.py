"""open_orphan_issues.py

A-5 (epic #1356): scripts/detect_orphan_pages.py の出力
(`data/analytics/orphan_pages.json`) を読み、内部リンク孤児の各 URL について
「内部リンク強化」提案 Issue を per-URL で起票する。

重複防止マーカー: `<!-- a5-orphan:<URL> -->`
ラベル: quality,todo,analytics

A-4 (engagement_drop) と分離する理由:
- 問題が違う (内部リンク構造 ≠ 本文の読み応え)
- アクションが違う (他ページから当該ページへの inbound リンク追加)
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
logger = logging.getLogger("open_orphan_issues")

DEFAULT_IN = "data/analytics/orphan_pages.json"
MARKER_PREFIX = "a5-orphan:"
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
    eng = d.get("engagement_rate")
    eng_s = f"{eng*100:.1f}%" if eng is not None else "(n/a)"
    parts = [
        f"<!-- {MARKER_PREFIX}{url} -->",
        "親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## 内部リンク孤児 概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{url}` |",
        f"| 観察期間 | {rng} |",
        f"| PV | {d['screen_page_views']} |",
        f"| entrances (着地数) | {d['entrances']} |",
        f"| 内部流入 PV (PV - entrances) | {d['internal_pageviews']} |",
        f"| entrance ratio | {d['entrance_ratio']*100:.1f}% |",
        f"| engagement rate | {eng_s} |",
        "",
        "## アクション提案",
        "",
        f"このページは閲覧のほぼ全て ({d['entrance_ratio']*100:.1f}%) が検索 / 外部からの"
        f"着地で、サイト内の他ページからの内部流入が {d['internal_pageviews']} PV しかない。"
        "内部リンクで指されていない「孤児」状態で、回遊・リンクエクイティ分配・"
        "クロール頻度のいずれも損している。**他ページから本ページへの inbound リンク**を増やす。",
        "",
        "1. **関連 brand hub / pillar から張る** — 同テーマのハブ記事の本文 / 関連リンク欄に追加",
        "2. **同カテゴリ記事の本文中リンク** — 文脈の合う既存記事から context リンクを向ける",
        "3. **関連記事ブロック** — 近いトピックの記事末尾「関連記事」に本ページを含める",
        "4. **taxonomy / 一覧での露出** — 適切なタグ / カテゴリに属しているか確認し、一覧経由の導線を確保",
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
        logger.info("no orphan pages — exiting")
        return 0

    taken = find_existing_taken(args.repo)
    logger.info("existing orphan-page open Issues: %d", len(taken))

    created = 0
    for d in detected:
        url = d["url"]
        if url in taken:
            logger.info("skip (already open): %s", url)
            continue
        title = (f"[A-5] Internal-link orphan: {url} "
                 f"(entrance {d['entrance_ratio']*100:.0f}%, internal={d['internal_pageviews']})")
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
