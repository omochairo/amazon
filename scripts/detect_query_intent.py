"""detect_query_intent.py

GSC weekly JSON の query×page (`by_combo`) から、各ページに流入している検索クエリ群を
informational / commercial / navigational の 3 意図に分類し、ページ単位の「主要意図」と
その意図に適した CTA レイアウト推奨を `data/analytics/query_intent.json` に書き出す
read-only スクリプト (A-7, epic #1356)。

意図分類 (日本語キーワードヒューリスティック、クエリ毎に impressions で重み付け):
- navigational: 公式 / 店舗 / ログイン 等 (最も具体的なので最初に判定)
- commercial : おすすめ / 比較 / ランキング / 価格 / レビュー / 購入 等 (買い手意図)
- informational: 上記以外 (とは / 方法 / 何歳 / 効果 等の知りたい意図)

CTA 推奨:
- commercial   → 購入 CTA (価格 + Amazon/楽天ボタン + 比較表) を above-the-fold で強調
- informational→ 結論先出し + 関連記事/比較、購入 CTA は本文中盤以降に控えめ
- navigational → ブランドハブ / 公式情報への導線を上部に

完了基準の「副作用ゼロ」を守るため、本スクリプトは分類結果を出力するのみ。
build_post の CTA レイアウト実変更は別 PR/epic に委ね、ここでは per-page の
「意図と推奨 CTA」を提案 Issue 化する材料を作る。

副作用ゼロ。記事生成パイプラインに触れない。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-7)
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
logger = logging.getLogger("detect_query_intent")

DEFAULT_IN = "data/analytics/gsc_weekly.json"
DEFAULT_OUT = "data/analytics/query_intent.json"
DEFAULT_MIN_IMPRESSIONS = 50
DEFAULT_MIN_DOMINANT_SHARE = 0.60
DEFAULT_MAX_RESULTS = 10
DEFAULT_TOP_QUERIES = 8

NAVIGATIONAL_KW = ["公式", "店舗", "ログイン", "アクセス", "マイページ", "会員"]
COMMERCIAL_KW = [
    "おすすめ", "オススメ", "比較", "ランキング", "選び方", "人気", "最安", "価格",
    "値段", "安い", "通販", "購入", "買う", "セール", "レビュー", "口コミ", "評価",
    "評判", "セット", "どっち", "コスパ",
]

CTA_RECOMMENDATION = {
    "commercial": "購入 CTA (価格 + Amazon/楽天ボタン + 比較表) を above-the-fold で強調。"
                  "買い手意図のクエリが主流なので、結論 (買うべき商品) を早く提示する。",
    "informational": "冒頭で結論/答えを先出しし、比較表・関連記事で滞在を伸ばす。"
                     "購入 CTA は本文中盤以降に控えめに配置 (情報目的の離脱を防ぐ)。",
    "navigational": "ブランドハブ / 公式情報への導線をページ上部に。"
                    "指名検索なので、目的の情報 (ラインナップ/型番/購入先) に最短で到達させる。",
}


def classify_query(query: str) -> str:
    q = query or ""
    for kw in NAVIGATIONAL_KW:
        if kw in q:
            return "navigational"
    for kw in COMMERCIAL_KW:
        if kw in q:
            return "commercial"
    return "informational"


def detect(gsc: dict[str, Any], *,
           min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
           min_dominant_share: float = DEFAULT_MIN_DOMINANT_SHARE,
           max_results: int = DEFAULT_MAX_RESULTS,
           top_queries: int = DEFAULT_TOP_QUERIES) -> dict[str, Any]:
    # page -> {intent -> impressions}, page -> [query rows]
    per_page_intent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_page_queries: dict[str, list[dict]] = defaultdict(list)
    for row in gsc.get("by_combo", []):
        page = row.get("page")
        query = row.get("query")
        if not page or not query:
            continue
        imp = row.get("impressions", 0)
        intent = classify_query(query)
        per_page_intent[page][intent] += imp
        per_page_queries[page].append({
            "query": query,
            "intent": intent,
            "impressions": imp,
            "clicks": row.get("clicks", 0),
            "position": row.get("position", 0.0),
        })

    detected = []
    for page, intent_imp in per_page_intent.items():
        total = sum(intent_imp.values())
        if total < min_impressions:
            continue
        dominant = max(intent_imp.items(), key=lambda kv: kv[1])
        dominant_intent, dominant_imp = dominant
        share = dominant_imp / total if total else 0.0
        if share < min_dominant_share:
            # 意図が割れているページは「混在」として扱い、明確な推奨を出さない
            continue
        qs = sorted(per_page_queries[page], key=lambda r: r["impressions"], reverse=True)
        detected.append({
            "page": page,
            "dominant_intent": dominant_intent,
            "dominant_share": round(share, 3),
            "total_impressions": total,
            "intent_breakdown": {k: v for k, v in sorted(
                intent_imp.items(), key=lambda kv: kv[1], reverse=True)},
            "cta_recommendation": CTA_RECOMMENDATION[dominant_intent],
            "top_queries": qs[:top_queries],
        })

    detected.sort(key=lambda r: r["total_impressions"], reverse=True)
    detected = detected[:max_results]

    return {
        "source_range": gsc.get("range"),
        "params": {
            "min_impressions": min_impressions,
            "min_dominant_share": min_dominant_share,
            "max_results": max_results,
        },
        "detected": detected,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    p.add_argument("--min-dominant-share", type=float, default=DEFAULT_MIN_DOMINANT_SHARE)
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
        min_dominant_share=args.min_dominant_share,
        max_results=args.max_results,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d classified pages)", out, len(result["detected"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
