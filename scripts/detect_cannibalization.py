"""detect_cannibalization.py

GSC weekly JSON の query×page (`by_combo`) から「クエリ・カニバリゼーション」
(同一クエリで複数 URL が impressions を奪い合っている状態) を検出し、
`data/analytics/cannibalization.json` に書き出す read-only スクリプト
(A-3, epic #1356)。

カニバリの害:
- 同じ検索意図に対して複数ページが competing し、被リンク / 内部リンク /
  topical authority が分散 → どのページも 1 ページ目に届きにくい
- Google が「どれを出すべきか」を都度入れ替える → 順位が不安定化

検出条件 (どれも満たすクエリのみ candidate):
- そのクエリで impressions >= MIN_PAGE_IMPRESSIONS の URL が COMPETING_PAGES (default 2) 以上
- クエリ全体の impressions 合計 >= MIN_QUERY_IMPRESSIONS
- 上位 1 位 URL の impressions シェアが DOMINANCE_MAX 未満
  (1 ページが圧倒的なら実質カニバってない → 除外)

ランキング: competing ページ群の impressions 合計の降順、top N。

副作用ゼロ。記事生成パイプラインに触れない。提案 Issue 経由のみ。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-3)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from collections import defaultdict
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_cannibalization")

DEFAULT_IN = "data/analytics/gsc_weekly.json"
DEFAULT_OUT = "data/analytics/cannibalization.json"
DEFAULT_MIN_PAGE_IMPRESSIONS = 10
DEFAULT_MIN_QUERY_IMPRESSIONS = 50
DEFAULT_COMPETING_PAGES = 2
DEFAULT_DOMINANCE_MAX = 0.85
DEFAULT_MAX_RESULTS = 10


def detect(gsc: dict[str, Any], *,
           min_page_impressions: int = DEFAULT_MIN_PAGE_IMPRESSIONS,
           min_query_impressions: int = DEFAULT_MIN_QUERY_IMPRESSIONS,
           competing_pages: int = DEFAULT_COMPETING_PAGES,
           dominance_max: float = DEFAULT_DOMINANCE_MAX,
           max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    by_query: dict[str, list[dict]] = defaultdict(list)
    for row in gsc.get("by_combo", []):
        query = row.get("query")
        page = row.get("page")
        if not query or not page:
            continue
        by_query[query].append({
            "page": page,
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0.0),
            "position": row.get("position", 0.0),
        })

    detected = []
    for query, pages in by_query.items():
        competing = [p for p in pages if p["impressions"] >= min_page_impressions]
        if len(competing) < competing_pages:
            continue
        total_imp = sum(p["impressions"] for p in competing)
        if total_imp < min_query_impressions:
            continue
        competing.sort(key=lambda p: p["impressions"], reverse=True)
        top_share = competing[0]["impressions"] / total_imp if total_imp else 1.0
        if top_share >= dominance_max:
            # 1 ページが圧倒的 — 実質カニバっていない
            continue
        detected.append({
            "query": query,
            "competing_page_count": len(competing),
            "total_impressions": total_imp,
            "total_clicks": sum(p["clicks"] for p in competing),
            "top_share": round(top_share, 3),
            "best_position": min(p["position"] for p in competing),
            "pages": competing,
        })

    detected.sort(key=lambda r: r["total_impressions"], reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": gsc.get("range"),
        "params": {
            "min_page_impressions": min_page_impressions,
            "min_query_impressions": min_query_impressions,
            "competing_pages": competing_pages,
            "dominance_max": dominance_max,
            "max_results": max_results,
        },
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-page-impressions", type=int, default=DEFAULT_MIN_PAGE_IMPRESSIONS)
    p.add_argument("--min-query-impressions", type=int, default=DEFAULT_MIN_QUERY_IMPRESSIONS)
    p.add_argument("--competing-pages", type=int, default=DEFAULT_COMPETING_PAGES)
    p.add_argument("--dominance-max", type=float, default=DEFAULT_DOMINANCE_MAX)
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    gsc = json.loads(in_path.read_text(encoding="utf-8"))

    result = detect(
        gsc,
        min_page_impressions=args.min_page_impressions,
        min_query_impressions=args.min_query_impressions,
        competing_pages=args.competing_pages,
        dominance_max=args.dominance_max,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d cannibalization candidates)", out, len(result["detected"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
