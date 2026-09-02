"""open_opportunity_issues.py

A-2 (epic #1356): scripts/detect_opportunity_pages.py の出力
(`data/analytics/opportunity_pages.json`) を読み、各 URL について
「2 ページ目 → 1 ページ目押し上げ候補」Issue を per-URL で起票する。

重複防止マーカー: `<!-- a2-opp-page:<URL> -->`
ラベル: quality,todo,analytics

A-1 (open_low_ctr_issues.py) と分離している理由:
- アクション提案が違う (CTR 改善 ≠ 内部リンク/H2 強化)
- 同 URL に対して両方の Issue が同時に存在し得る (CTR 低 & 機会記事 の場合)
- ラベル + marker prefix で混在防止
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
logger = logging.getLogger("open_opportunity_issues")

DEFAULT_IN = "data/analytics/opportunity_pages.json"
MARKER_PREFIX = "a2-opp-page:"
LABELS = "quality,todo,analytics"


def find_existing_taken_urls(repo: str) -> set[str]:
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


def render_query_rows(top_queries: list[dict]) -> str:
    if not top_queries:
        return "| (no per-query data) | - | - | - | - |"
    return "\n".join(
        f"| `{q['query']}` | {q['impressions']} | {q['clicks']} | "
        f"{q['ctr']*100:.2f}% | {q['position']:.1f} |"
        for q in top_queries
    )


def render_body(detected: dict, *, src_range: dict) -> str:
    page = detected["page"]
    imp = detected["impressions"]
    clicks = detected["clicks"]
    ctr = detected["ctr"]
    pos = detected["position"]
    top_q = detected.get("top_queries") or []
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"

    parts = [
        f"<!-- {MARKER_PREFIX}{page} -->",
        *([m] if (m := expiry_marker(src_range)) else []),
        f"親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## 機会概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{page}` |",
        f"| 観察期間 | {rng} |",
        f"| impressions | {imp} |",
        f"| clicks | {clicks} |",
        f"| CTR | {ctr*100:.2f}% |",
        f"| avg position | {pos:.1f} (2 ページ目) |",
        "",
        f"## 検索流入クエリ Top {len(top_q)}",
        "",
        "| クエリ | impressions | clicks | CTR | position |",
        "|---|---:|---:|---:|---:|",
        render_query_rows(top_q),
        "",
        "## アクション提案",
        "",
        f"このページは position {pos:.1f} (2 ページ目) で impressions {imp} を獲得しているが、"
        "現状ほぼクリックされていない。1 ページ目に押し上げれば一気にトラフィックを取れる候補。",
        "",
        "1. **内部リンク強化** — 関連 brand hub / pillar / 上位 H2 から本記事に向けて context リンクを追加",
        "2. **H2/H3 改善** — 上記検索クエリ語を H2/H3 タイトルに取り込み topical relevance を上げる",
        "3. **関連記事セクション** — 記事末尾に「関連記事」ブロックを追加して dwell time / pages-per-session を改善",
        "4. **画像 alt 強化** — 画像 alt に検索クエリ語を入れて画像検索からの追加流入も狙う",
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{page} -->` マーカーを埋め込み済み",
        "- 同 URL が再検出されてもこの Issue が open なら新規起票なし",
        "- 1 ページ目に到達 (position <= 10) して 2 週間維持されたら自動 close を将来検討",
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
        logger.info("no opportunity pages — exiting")
        return 0

    taken = find_existing_taken_urls(args.repo)
    logger.info("existing opportunity-page open Issues: %d", len(taken))

    created = 0
    for d in detected:
        page = d["page"]
        if page in taken:
            logger.info("skip (already open): %s", page)
            continue
        title = (f"[A-2] Opportunity page (pos {d['position']:.1f}): {page} "
                 f"(imp={d['impressions']}, clicks={d['clicks']})")
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
