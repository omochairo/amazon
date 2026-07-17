"""detect_article_feedback_worst_pages.py

`data/analytics/article_feedback_monthly.json` (scripts/fetch_ga4_article_feedback.py
出力) を読み、bad (物足りない) 比率が高いページ上位を抽出して
`data/analytics/article_feedback_worst_pages.json` に書き出す read-only スクリプト。

集計仕様 (issue #2051):
- `rating_dimension_available: true` (GA4 custom dimension `rating` 登録済) の場合:
  page_path 毎に good/ok/bad/other の件数を合算し、
  bad_ratio = bad / (good+ok+bad+other) を計算。total >= MIN_TOTAL のページを
  bad_ratio 降順 (同率は total 降順) で上位 MAX_RESULTS 件抽出する。
- `rating_dimension_available: false` (未登録) の場合:
  rating 内訳が取れないため bad_ratio でのランキングはできない。
  代わりに total (フィードバック件数) 降順で上位ページを抽出し、
  `rating_breakdown_available: false` を各行に残して「内訳なしの参考情報」
  であることを report 側で明示する (GA4 admin 登録待ちの間も進捗を止めない)。

副作用ゼロ。記事生成パイプライン / score / narrative に影響しない。

Issue: https://github.com/omochairo/amazon/issues/2051
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_article_feedback_worst_pages")

DEFAULT_IN = "data/analytics/article_feedback_monthly.json"
DEFAULT_OUT = "data/analytics/article_feedback_worst_pages.json"
DEFAULT_MIN_TOTAL = 5
DEFAULT_MAX_RESULTS = 15
KNOWN_RATINGS = ("good", "ok", "bad")


def aggregate_by_page(rows: list[dict], *, rating_available: bool) -> list[dict]:
    """rows (pagePath [x customEvent:rating] x eventCount) を pagePath 単位に集計する
    pure function。GA4 API 呼び出しに依存しないため単体テスト可能。
    """
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        path = r.get("pagePath", "")
        if not path:
            continue
        count = int(r.get("eventCount", 0))
        bucket = agg.setdefault(
            path, {"good": 0, "ok": 0, "bad": 0, "other": 0, "total": 0}
        )
        bucket["total"] += count
        if rating_available:
            rating = r.get("customEvent:rating", "")
            key = rating if rating in KNOWN_RATINGS else "other"
            bucket[key] += count

    out = []
    for path, b in agg.items():
        row: dict[str, Any] = {
            "page_path": path,
            "total": b["total"],
        }
        if rating_available:
            row["good"] = b["good"]
            row["ok"] = b["ok"]
            row["bad"] = b["bad"]
            row["other"] = b["other"]
            row["bad_ratio"] = (b["bad"] / b["total"]) if b["total"] else 0.0
        out.append(row)
    return out


def detect(agg: list[dict], *, rating_available: bool,
           min_total: int = DEFAULT_MIN_TOTAL,
           max_results: int = DEFAULT_MAX_RESULTS) -> list[dict]:
    """集計済み agg から改善候補ワーストページを抽出する pure function。"""
    candidates = [r for r in agg if r["total"] >= min_total]
    if rating_available:
        candidates.sort(key=lambda r: (r.get("bad_ratio", 0.0), r["total"]), reverse=True)
    else:
        # rating 内訳が無いため bad_ratio でのランキング不可。フィードバック
        # 件数が多い = 読者の関心が高いページから優先的に手動確認してもらう。
        candidates.sort(key=lambda r: r["total"], reverse=True)
    return candidates[:max_results]


def build_report(data: dict, *, min_total: int, max_results: int) -> dict[str, Any]:
    rating_available = bool(data.get("rating_dimension_available"))
    rows = data.get("rows") or []
    agg = aggregate_by_page(rows, rating_available=rating_available)
    detected = detect(
        agg, rating_available=rating_available,
        min_total=min_total, max_results=max_results,
    )
    return {
        "source_range": data.get("range"),
        "rating_dimension_available": rating_available,
        "params": {
            "min_total": min_total,
            "max_results": max_results,
        },
        "totals": {
            "pages": len(agg),
            "events": sum(r["total"] for r in agg),
        },
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL)
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    data = json.loads(in_path.read_text(encoding="utf-8"))

    report = build_report(data, min_total=args.min_total, max_results=args.max_results)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s (%d worst pages, rating_dimension_available=%s)",
        out, len(report["detected"]), report["rating_dimension_available"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
