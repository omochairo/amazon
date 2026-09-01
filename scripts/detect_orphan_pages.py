"""detect_orphan_pages.py

GA4 weekly JSON の by_page から「内部リンク孤児」(ほぼ全 PV が検索 / 外部からの
着地で、サイト内の他ページからほとんど流入が無い記事) を検出し、
`data/analytics/orphan_pages.json` に書き出す read-only スクリプト (A-5, epic #1356)。

孤児の定義 (GA4 entrances ベース):
- entrances / screenPageViews が 1.0 に近い = そのページの閲覧はほぼ全てが
  セッション開始 (着地) → サイト内の他ページからの内部流入 (PV - entrances) が
  ほぼゼロ → 内部リンクで指されていない「孤児」状態
- 内部リンクを張れば回遊 / リンクエクイティ分配 / クロール頻度が改善する候補

検出条件 (すべて満たすページのみ candidate):
- hostName == TARGET_HOST (default navi.omcha.jp)
- pagePath が CONTENT_PREFIXES (/posts/, /products/) のいずれかで始まる
  (内部リンクで指されるべき本文ページに限定。ホーム / ハブ / 一覧は対象外)
- screenPageViews >= MIN_PV (default 50)
- entrance_ratio = entrances / screenPageViews >= MIN_ENTRANCE_RATIO (default 0.90)

A-4 (engagement_drop) との違い:
- A-4 = 流入後ほぼ読まれず即離脱 (本文の読み応え問題)
- A-5 = そもそも内部流入が無い (内部リンク構造の問題)。読まれているかは問わない

副作用ゼロ。記事生成パイプラインに触れない。提案 Issue 経由のみ。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-5)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_orphan_pages")

DEFAULT_IN = "data/analytics/ga4_weekly.json"
DEFAULT_OUT = "data/analytics/orphan_pages.json"
DEFAULT_TARGET_HOST = "navi.omcha.jp"
DEFAULT_MIN_PV = 50
DEFAULT_MIN_ENTRANCE_RATIO = 0.90
DEFAULT_MAX_RESULTS = 10
CONTENT_PREFIXES = ("/posts/", "/products/")


def detect(ga4: dict[str, Any], *,
           target_host: str = DEFAULT_TARGET_HOST,
           min_pv: int = DEFAULT_MIN_PV,
           min_entrance_ratio: float = DEFAULT_MIN_ENTRANCE_RATIO,
           max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    detected = []
    eligible = 0
    for row in ga4.get("by_page", []):
        host = row.get("hostName", "")
        path = row.get("pagePath", "")
        if host != target_host or not path:
            continue
        if not path.startswith(CONTENT_PREFIXES):
            continue
        pv = row.get("screenPageViews", 0)
        entrances = row.get("entrances")
        # entrances が無い (古い artifact 等) 行は誤検出回避のため skip
        if entrances is None or pv < min_pv:
            continue
        eligible += 1
        ratio = entrances / pv if pv else 0.0
        if ratio < min_entrance_ratio:
            continue
        detected.append({
            "url": f"https://{host}{path}",
            "page_path": path,
            "screen_page_views": pv,
            "entrances": entrances,
            "internal_pageviews": max(pv - entrances, 0),
            "entrance_ratio": round(ratio, 4),
            "engagement_rate": round(row.get("engagementRate"), 4)
            if row.get("engagementRate") is not None else None,
        })

    # entrance_ratio が高く PV も多いものを優先 (孤児度 × 影響度)
    detected.sort(key=lambda r: (r["entrance_ratio"], r["screen_page_views"]), reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": ga4.get("range"),
        "params": {
            "target_host": target_host,
            "min_pv": min_pv,
            "min_entrance_ratio": min_entrance_ratio,
            "max_results": max_results,
        },
        "eligible": eligible,
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--target-host", default=DEFAULT_TARGET_HOST)
    p.add_argument("--min-pv", type=int, default=DEFAULT_MIN_PV)
    p.add_argument("--min-entrance-ratio", type=float, default=DEFAULT_MIN_ENTRANCE_RATIO)
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    ga4 = json.loads(in_path.read_text(encoding="utf-8"))

    result = detect(
        ga4,
        target_host=args.target_host,
        min_pv=args.min_pv,
        min_entrance_ratio=args.min_entrance_ratio,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d orphan pages)", out, len(result["detected"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
