"""detect_opportunity_pages.py

GSC weekly JSON から「2 ページ目に居て 1 ページ目に押し上げられそうな
機会記事」を抽出し、`data/analytics/opportunity_pages.json` に書き出す
read-only スクリプト (A-2, epic #1356)。

検出条件:
- 11.0 <= position <= MAX_POSITION (default 20.0)
- impressions >= MIN_IMPRESSIONS (default 100, A-1 より高めに設定して機会度の強い記事に絞る)
- top_n (default 5) のみ出力

機会記事の typical な改善経路:
- 内部リンク追加 (関連 brand hub / pillar から流入を厚くする)
- H2/H3 に検索クエリ語を取り込み topical relevance を上げる
- 関連記事セクションを末尾に追加

副作用ゼロ。記事生成パイプラインに触れない。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-2)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_opportunity_pages")

DEFAULT_IN = "data/analytics/gsc_weekly.json"
DEFAULT_OUT = "data/analytics/opportunity_pages.json"
DEFAULT_MIN_IMPRESSIONS = 100
DEFAULT_MIN_POSITION = 11.0
DEFAULT_MAX_POSITION = 20.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_TOP_QUERIES_PER_PAGE = 5


def collect_top_queries_by_page(by_combo: list[dict], n: int) -> dict[str, list[dict]]:
    """page -> top N クエリ。A-1 と同形式。"""
    grouped: dict[str, list[dict]] = {}
    for row in by_combo:
        page = row.get("page")
        if not page:
            continue
        grouped.setdefault(page, []).append({
            "query": row.get("query", ""),
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0.0),
            "position": row.get("position", 0.0),
        })
    for page, qs in grouped.items():
        qs.sort(key=lambda r: r["impressions"], reverse=True)
        grouped[page] = qs[:n]
    return grouped


def detect(gsc: dict[str, Any], *,
           min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
           min_position: float = DEFAULT_MIN_POSITION,
           max_position: float = DEFAULT_MAX_POSITION,
           max_results: int = DEFAULT_MAX_RESULTS,
           top_queries_per_page: int = DEFAULT_TOP_QUERIES_PER_PAGE) -> dict[str, Any]:
    queries_by_page = collect_top_queries_by_page(
        gsc.get("by_combo", []), top_queries_per_page,
    )

    detected = []
    for row in gsc.get("by_page", []):
        page = row.get("page")
        impressions = row.get("impressions", 0)
        position = row.get("position", 0.0)
        if not page or impressions < min_impressions:
            continue
        if position < min_position or position > max_position:
            continue
        detected.append({
            "page": page,
            "clicks": row.get("clicks", 0),
            "impressions": impressions,
            "ctr": row.get("ctr", 0.0),
            "position": position,
            "top_queries": queries_by_page.get(page, []),
        })

    detected.sort(key=lambda r: r["impressions"], reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": gsc.get("range"),
        "params": {
            "min_impressions": min_impressions,
            "min_position": min_position,
            "max_position": max_position,
            "max_results": max_results,
        },
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    p.add_argument("--min-position", type=float, default=DEFAULT_MIN_POSITION)
    p.add_argument("--max-position", type=float, default=DEFAULT_MAX_POSITION)
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    gsc = json.loads(in_path.read_text(encoding="utf-8"))

    result = detect(
        gsc,
        min_impressions=args.min_impressions,
        min_position=args.min_position,
        max_position=args.max_position,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d opportunity pages)", out, len(result["detected"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
