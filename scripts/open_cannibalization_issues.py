"""open_cannibalization_issues.py

A-3 (epic #1356): scripts/detect_cannibalization.py の出力
(`data/analytics/cannibalization.json`) を読み、カニバっている各クエリについて
「canonical 統合 / 内部リンク集約」提案 Issue を per-query で起票する。

重複防止マーカー: `<!-- a3-cannibal:<query> -->`
ラベル: quality,todo,analytics

A-1/A-2 と分離している理由:
- アクション提案が違う (URL 単体改善ではなく URL 群の統合 / 役割分担)
- marker prefix が異なるので per-query で独立 dedup される
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys

from scripts._analytics_issue_expiry import expiry_marker, expiry_note

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_cannibalization_issues")

DEFAULT_IN = "data/analytics/cannibalization.json"
MARKER_PREFIX = "a3-cannibal:"
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
            key = tail.split("-->", 1)[0].strip()
            if key:
                taken.add(key)
            idx = body.find(MARKER_PREFIX, idx + 1)
    return taken


def render_page_rows(pages: list[dict]) -> str:
    return "\n".join(
        f"| `{p['page']}` | {p['impressions']} | {p['clicks']} | "
        f"{p['ctr']*100:.2f}% | {p['position']:.1f} |"
        for p in pages
    )


def render_body(d: dict, *, src_range: dict) -> str:
    query = d["query"]
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    pages = d.get("pages") or []
    parts = [
        f"<!-- {MARKER_PREFIX}{query} -->",
        *([m] if (m := expiry_marker(src_range)) else []),
        "親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## カニバリ概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| クエリ | `{query}` |",
        f"| 観察期間 | {rng} |",
        f"| 競合 URL 数 | {d['competing_page_count']} |",
        f"| 合計 impressions | {d['total_impressions']} |",
        f"| 合計 clicks | {d['total_clicks']} |",
        f"| 最上位 URL の impressions シェア | {d['top_share']*100:.1f}% |",
        f"| 最良 position | {d['best_position']:.1f} |",
        "",
        "## 競合している URL",
        "",
        "| URL | impressions | clicks | CTR | position |",
        "|---|---:|---:|---:|---:|",
        render_page_rows(pages),
        "",
        "## アクション提案",
        "",
        f"同一クエリ `{query}` に対して {d['competing_page_count']} 本の URL が "
        "impressions を奪い合っており、topical authority と内部リンクが分散している。"
        "どれか 1 本に検索意図を集約して 1 ページ目入りを狙う。",
        "",
        "1. **主役 URL を 1 本決める** — 上表で最も impressions / position の良い URL を canonical 候補に",
        "2. **役割分担 or 統合** — 内容が重複なら弱い方を主役へ 301 / canonical 集約。"
        "意図が違うなら H2 / タイトルを差別化して別クエリに振り分け",
        "3. **内部リンク集約** — 競合 URL 群から主役 URL へ向けて context リンクを張り、"
        "リンクエクイティを 1 本に寄せる",
        "4. **重複コンテンツ点検** — 同じ見出し / 同じ商品リストが複数ページに散っていないか確認",
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{query} -->` マーカーを埋め込み済み",
        "- 同クエリが再検出されてもこの Issue が open なら新規起票なし",
        *expiry_note(src_range),
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
        logger.info("no cannibalization candidates — exiting")
        return 0

    taken = find_existing_taken(args.repo)
    logger.info("existing cannibalization open Issues: %d", len(taken))

    created = 0
    for d in detected:
        query = d["query"]
        if query in taken:
            logger.info("skip (already open): %s", query)
            continue
        title = (f"[A-3] Query cannibalization: \"{query}\" "
                 f"({d['competing_page_count']} URLs, imp={d['total_impressions']})")
        body = render_body(d, src_range=src_range)
        if args.dry_run:
            logger.info("would create: %s", title)
            continue
        url = create_issue(args.repo, title, body)
        logger.info("created: %s", url)
        created += 1

    logger.info("created %d new Issue(s)", created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
