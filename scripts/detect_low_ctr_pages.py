"""detect_low_ctr_pages.py

GSC weekly JSON (scripts/fetch_gsc.py 出力) から CTR が site 平均を大きく
下回るページを抽出し、Jules リライト候補として `data/analytics/low_ctr_pages.json`
に書き出す read-only スクリプト。

検出条件 (A-1, epic #1356):
- position <= MAX_POSITION (default 10, page 1 のみ。position 11-20 は A-2 で別途扱う)
- impressions >= MIN_IMPRESSIONS (default 50, 統計的信号確保)
- ctr < max(baseline_ctr * RATIO, ABS_MIN_THRESHOLD)
  - baseline_ctr = totals.clicks_sum / totals.impressions_sum
  - RATIO default 0.5 (site 平均の半分以下)
  - ABS_MIN_THRESHOLD default 0.005 (baseline が極端に低い時の下駄)

出力 JSON: detected ページ毎に top クエリ (by_combo より) を 5 件まで付与。
workflow 側で重複 Issue 起票防止のためのマーカー `<!-- a1-low-ctr:URL -->` を
本文に埋め込む運用。

副作用ゼロ。記事生成パイプライン / score / narrative に影響しない。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-1)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_low_ctr_pages")

DEFAULT_IN = "data/analytics/gsc_weekly.json"
DEFAULT_OUT = "data/analytics/low_ctr_pages.json"
DEFAULT_MIN_IMPRESSIONS = 50
DEFAULT_MAX_POSITION = 10.0
DEFAULT_RATIO = 0.5
DEFAULT_ABS_MIN_THRESHOLD = 0.005
DEFAULT_MAX_RESULTS = 5
DEFAULT_TOP_QUERIES_PER_PAGE = 5


def compute_baseline_ctr(totals: dict[str, Any]) -> float:
    """site 全体の CTR baseline。impressions=0 なら 0 を返す。"""
    impressions = totals.get("impressions_sum", 0) or 0
    clicks = totals.get("clicks_sum", 0) or 0
    if impressions <= 0:
        return 0.0
    return clicks / impressions


def collect_top_queries_by_page(by_combo: list[dict], n: int) -> dict[str, list[dict]]:
    """page URL -> top N クエリ (impressions 降順) のマップを作る。"""
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
    for page, queries in grouped.items():
        queries.sort(key=lambda r: r["impressions"], reverse=True)
        grouped[page] = queries[:n]
    return grouped


def detect(gsc: dict[str, Any], *,
           min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
           max_position: float = DEFAULT_MAX_POSITION,
           ratio: float = DEFAULT_RATIO,
           abs_min_threshold: float = DEFAULT_ABS_MIN_THRESHOLD,
           max_results: int = DEFAULT_MAX_RESULTS,
           top_queries_per_page: int = DEFAULT_TOP_QUERIES_PER_PAGE) -> dict[str, Any]:
    """GSC weekly JSON から低 CTR ページを抽出する。"""
    totals = gsc.get("totals", {})
    baseline = compute_baseline_ctr(totals)
    threshold = max(baseline * ratio, abs_min_threshold)
    logger.info("baseline_ctr=%.4f, threshold=%.4f (ratio=%.2f, abs_min=%.4f)",
                baseline, threshold, ratio, abs_min_threshold)

    queries_by_page = collect_top_queries_by_page(gsc.get("by_combo", []), top_queries_per_page)

    detected = []
    for row in gsc.get("by_page", []):
        page = row.get("page")
        impressions = row.get("impressions", 0)
        position = row.get("position", 0.0)
        ctr = row.get("ctr", 0.0)
        if not page or impressions < min_impressions:
            continue
        if position <= 0 or position > max_position:
            continue
        if ctr >= threshold:
            continue
        detected.append({
            "page": page,
            "clicks": row.get("clicks", 0),
            "impressions": impressions,
            "ctr": ctr,
            "position": position,
            "ratio_to_baseline": (ctr / baseline) if baseline > 0 else None,
            "top_queries": queries_by_page.get(page, []),
        })

    detected.sort(key=lambda r: r["impressions"], reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": gsc.get("range"),
        "baseline_ctr": baseline,
        "threshold_ctr": threshold,
        "params": {
            "min_impressions": min_impressions,
            "max_position": max_position,
            "ratio": ratio,
            "abs_min_threshold": abs_min_threshold,
            "max_results": max_results,
        },
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    p.add_argument("--max-position", type=float, default=DEFAULT_MAX_POSITION)
    p.add_argument("--ratio", type=float, default=DEFAULT_RATIO)
    p.add_argument("--abs-min-threshold", type=float, default=DEFAULT_ABS_MIN_THRESHOLD)
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
        max_position=args.max_position,
        ratio=args.ratio,
        abs_min_threshold=args.abs_min_threshold,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d detected, baseline_ctr=%.4f)",
                out, len(result["detected"]), result["baseline_ctr"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
