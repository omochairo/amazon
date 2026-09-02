"""open_query_intent_issues.py

A-7 (epic #1356): scripts/detect_query_intent.py の出力
(`data/analytics/query_intent.json`) を読み、主要検索意図が明確なページについて
「意図に合わせた CTA レイアウト確認 / 差し替え」提案 Issue を per-URL で起票する。

重複防止マーカー: `<!-- a7-intent:<URL> -->`
ラベル: quality,todo,analytics

build_post の実変更は含まない (epic 完了基準=副作用ゼロ)。本 Issue は per-page の
意図と推奨 CTA を提示し、build_post / テンプレ側での CTA レイアウト調整を促す。
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
logger = logging.getLogger("open_query_intent_issues")

DEFAULT_IN = "data/analytics/query_intent.json"
MARKER_PREFIX = "a7-intent:"
LABELS = "quality,todo,analytics"
INTENT_JA = {
    "commercial": "商業 (買い手意図)",
    "informational": "情報収集",
    "navigational": "指名 (ナビゲーショナル)",
}


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


def render_breakdown(breakdown: dict) -> str:
    return "\n".join(
        f"| {INTENT_JA.get(k, k)} | {v} |" for k, v in breakdown.items()
    )


def render_query_rows(top_queries: list[dict]) -> str:
    if not top_queries:
        return "| (no per-query data) | - | - | - |"
    return "\n".join(
        f"| `{q['query']}` | {INTENT_JA.get(q['intent'], q['intent'])} | "
        f"{q['impressions']} | {q['position']:.1f} |"
        for q in top_queries
    )


def render_body(d: dict, *, src_range: dict) -> str:
    page = d["page"]
    intent = d["dominant_intent"]
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    parts = [
        f"<!-- {MARKER_PREFIX}{page} -->",
        *([m] if (m := expiry_marker(src_range)) else []),
        "親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## 検索意図 概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{page}` |",
        f"| 観察期間 | {rng} |",
        f"| 主要意図 | **{INTENT_JA.get(intent, intent)}** |",
        f"| 主要意図シェア | {d['dominant_share']*100:.1f}% |",
        f"| 合計 impressions | {d['total_impressions']} |",
        "",
        "## 意図別 impressions",
        "",
        "| 意図 | impressions |",
        "|---|---:|",
        render_breakdown(d.get("intent_breakdown", {})),
        "",
        "## 流入クエリ (意図ラベル付き)",
        "",
        "| クエリ | 意図 | impressions | position |",
        "|---|---|---:|---:|",
        render_query_rows(d.get("top_queries", [])),
        "",
        "## 推奨 CTA レイアウト",
        "",
        d["cta_recommendation"],
        "",
        "## アクション提案",
        "",
        f"このページの検索流入は **{INTENT_JA.get(intent, intent)}** が "
        f"{d['dominant_share']*100:.1f}% を占める。現状の CTA レイアウトがこの意図に"
        "合っているか確認し、ズレていれば上記推奨に寄せる (build_post / テンプレ調整は別 PR)。",
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{page} -->` マーカーを埋め込み済み",
        "- 同 URL が再検出されてもこの Issue が open なら新規起票なし",
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
        logger.info("no classified pages — exiting")
        return 0

    taken = find_existing_taken(args.repo)
    logger.info("existing query-intent open Issues: %d", len(taken))

    created = 0
    for d in detected:
        page = d["page"]
        if page in taken:
            logger.info("skip (already open): %s", page)
            continue
        title = (f"[A-7] Query intent = {d['dominant_intent']} "
                 f"({d['dominant_share']*100:.0f}%): {page}")
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
