"""detect_engagement_drop.py

GA4 weekly JSON の by_page から「engagement が落ちているページ」(PV はあるのに
ほとんど読まれず離脱している記事) を検出し、`data/analytics/engagement_drop.json`
に書き出す read-only スクリプト (A-4, epic #1356)。

検出条件 (すべて満たすページのみ candidate):
- hostName == TARGET_HOST (default navi.omcha.jp。omcha.jp は WordPress 別物のため除外)
- screenPageViews >= MIN_PV (default 100)
- engagementRate < MAX_ENGAGEMENT (default 0.30)
- averageSessionDuration < MAX_DURATION (default 15.0 秒)
- pagePath が除外対象 (ホーム "/" 等) でない

engagement drop の典型 = タイトル / SERP の期待と本文がズレている or 冒頭で
価値が伝わらず即離脱。Jules リライト (冒頭フック / above-the-fold 結論先出し /
内部リンク / 内容の厚み) の対象。

副作用ゼロ。記事生成パイプラインに触れない。提案 Issue 経由のみ。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-4)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_engagement_drop")

DEFAULT_IN = "data/analytics/ga4_weekly.json"
DEFAULT_OUT = "data/analytics/engagement_drop.json"
DEFAULT_TARGET_HOST = "navi.omcha.jp"
DEFAULT_MIN_PV = 100
DEFAULT_MAX_ENGAGEMENT = 0.30
DEFAULT_MAX_DURATION = 15.0
DEFAULT_MAX_RESULTS = 10
# 離脱が正常なページ (ホーム / 検索 / タグ一覧) は rewrite 対象外
EXCLUDE_PATHS = {"/", "/search/", "/tags/", "/categories/", "/brands/"}


def detect(ga4: dict[str, Any], *,
           target_host: str = DEFAULT_TARGET_HOST,
           min_pv: int = DEFAULT_MIN_PV,
           max_engagement: float = DEFAULT_MAX_ENGAGEMENT,
           max_duration: float = DEFAULT_MAX_DURATION,
           max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    detected = []
    eligible = 0
    for row in ga4.get("by_page", []):
        host = row.get("hostName", "")
        path = row.get("pagePath", "")
        if host != target_host or not path:
            continue
        if path in EXCLUDE_PATHS:
            continue
        pv = row.get("screenPageViews", 0)
        eng = row.get("engagementRate")
        dur = row.get("averageSessionDuration", 0.0)
        # engagementRate が無い (古い artifact 等) 行は誤検出回避のため skip
        if eng is None:
            continue
        if pv < min_pv:
            continue
        eligible += 1
        if eng >= max_engagement or dur >= max_duration:
            continue
        detected.append({
            "url": f"https://{host}{path}",
            "page_path": path,
            "screen_page_views": pv,
            "engaged_sessions": row.get("engagedSessions", 0),
            "engagement_rate": round(eng, 4),
            "avg_session_duration": round(dur, 1),
            "bounce_rate": round(row.get("bounceRate", 0.0), 4),
        })

    # 影響度 = PV が多いほど優先 (改善インパクト大)
    detected.sort(key=lambda r: r["screen_page_views"], reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": ga4.get("range"),
        "params": {
            "target_host": target_host,
            "min_pv": min_pv,
            "max_engagement": max_engagement,
            "max_duration": max_duration,
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
    p.add_argument("--max-engagement", type=float, default=DEFAULT_MAX_ENGAGEMENT)
    p.add_argument("--max-duration", type=float, default=DEFAULT_MAX_DURATION)
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
        max_engagement=args.max_engagement,
        max_duration=args.max_duration,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d engagement-drop pages)", out, len(result["detected"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
