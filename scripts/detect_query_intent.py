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

出力は build_post.py が読み、front matter の `cta_layout` に反映する (#1980)。

**検出は週次スナップショットだが、出力は ledger として持ち越す (brain#47/#46/#45)。**
min_impressions=50 の週次窓は navi の実寸ではほとんどのページが跨げず、ある週に
分類されたページが翌週は窓に入らない。素の上書きだと `detected` から消え、
build_post が cta_layout を front matter に書かなくなる = 適用した CTA
レイアウトが 1 週で既定へ戻る。実際に 11 週の tracked 履歴でそうなっていた:

    2026-07-23  b0875fv2bq            → 07-30 で消える
    2026-08-13  /english-toys/        → 08-20 で消える
    2026-09-03  b0hdb8kg6g, b0h7mkjq9g→ 09-10 で消える
    2026-09-10  b010cqeucu, b0h4ppv8mr, /english-toys/ → 09-17 で消える

欠測 (窓に入らなかった) を「意図が変わった」と読んでしまうのが原因で、
amazon#7953 (在庫文言の欠測でタイトルが日替わり) と同じ型。処方も同じで、
**新しい観測があったときだけ上書きし、欠測では剥がさない**:

- 今週分類できたページ … そのまま採用 (意図が変わっていれば上書き = 反証)
- 今週窓に入らなかったページ … `carried_forward: true` で据え置き
- ただし `sticky_weeks` (default 8) 週続けて再確認できなければ落とす
  (剥がさないことと、永久に据え置くことは別)

記事生成パイプライン / score / narrative には触れない。

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
# 再確認できないまま持ち越せる上限 (週)。これを超えたページは ledger から落とす。
# 8 週 = 約 2 ヶ月。navi の週次 imp レンジだと 50 を跨ぐ週が数週に 1 度しか来ない
# ページがあり、4 週では「まだ現役だが窓に入らなかっただけ」を落としてしまう。
DEFAULT_STICKY_WEEKS = 8

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


def _load_previous(path: pathlib.Path) -> dict[str, Any]:
    """前回の出力 (tracked な data/analytics/query_intent.json) を読む。

    ファイル不在・空・parse 失敗・想定外の型は「前回は無かった」として扱う
    (持ち越しが無いだけで、今週の検出結果はそのまま出る)。
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("previous ledger unreadable, starting fresh: %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _carry_forward(previous: dict[str, Any], fresh_pages: set[str], *,
                   range_end: str | None,
                   sticky_weeks: int) -> tuple[list[dict], list[str]]:
    """前回の ledger から、今週再確認できなかったページを持ち越す。

    返すのは (持ち越すエントリ, 期限切れで落としたページ URL)。同じ週を
    再実行した場合 (previous の source_range.end が今回と同じ) は
    weeks_since_confirmed を増やさない — run が冪等であってほしいため。
    """
    prev_end = ((previous.get("source_range") or {}) if isinstance(
        previous.get("source_range"), dict) else {}).get("end")
    is_rerun = bool(range_end) and prev_end == range_end

    carried: list[dict] = []
    expired: list[str] = []
    for row in previous.get("detected", []) or []:
        if not isinstance(row, dict):
            continue
        page = row.get("page")
        if not page or page in fresh_pages:
            continue
        weeks = int(row.get("weeks_since_confirmed", 0) or 0)
        if not is_rerun:
            weeks += 1
        if weeks > sticky_weeks:
            expired.append(page)
            continue
        entry = dict(row)
        entry["carried_forward"] = True
        entry["weeks_since_confirmed"] = weeks
        carried.append(entry)
    return carried, expired


def detect(gsc: dict[str, Any], *,
           min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
           min_dominant_share: float = DEFAULT_MIN_DOMINANT_SHARE,
           max_results: int = DEFAULT_MAX_RESULTS,
           top_queries: int = DEFAULT_TOP_QUERIES,
           previous: dict[str, Any] | None = None,
           sticky_weeks: int = DEFAULT_STICKY_WEEKS) -> dict[str, Any]:
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

    range_end = (gsc.get("range") or {}).get("end")
    previous = previous or {}
    prev_by_page = {
        row.get("page"): row
        for row in (previous.get("detected") or [])
        if isinstance(row, dict) and row.get("page")
    }
    for row in detected:
        prev = prev_by_page.get(row["page"]) or {}
        # first_detected は「いつから cta_layout が当たっているか」の起点。
        # #6812 (切替の有効性検証) が前後比較の基準日として要る。
        row["first_detected"] = prev.get("first_detected") or range_end
        row["last_confirmed"] = range_end
        row["weeks_since_confirmed"] = 0
        row["carried_forward"] = False

    # max_results の打ち切りは今週の検出分にだけ当てる。持ち越し分まで
    # 同じ上限で切ると、今週たまたま検出が多い週に既存ページの cta_layout が
    # 剥がれ、ledger にした意味が無くなる。
    carried, expired = _carry_forward(
        previous, {row["page"] for row in detected},
        range_end=range_end, sticky_weeks=sticky_weeks)
    carried.sort(key=lambda r: r.get("total_impressions", 0), reverse=True)
    for page in expired:
        logger.info("dropped from ledger (not re-confirmed for >%d weeks): %s",
                    sticky_weeks, page)

    return {
        "source_range": gsc.get("range"),
        "params": {
            "min_impressions": min_impressions,
            "min_dominant_share": min_dominant_share,
            "max_results": max_results,
            "sticky_weeks": sticky_weeks,
        },
        "detected": detected + carried,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    p.add_argument("--min-dominant-share", type=float, default=DEFAULT_MIN_DOMINANT_SHARE)
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    p.add_argument("--previous", default=None,
                   help="前回の ledger (省略時は --out と同じパス。17-analytics-report は "
                        "tracked な query_intent.json を checkout 済みなので既定で持ち越せる)")
    p.add_argument("--sticky-weeks", type=int, default=DEFAULT_STICKY_WEEKS,
                   help="再確認できないまま持ち越せる上限 (週)。0 で持ち越し無効 (旧挙動)")
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    gsc = json.loads(in_path.read_text(encoding="utf-8"))

    out = pathlib.Path(args.out)
    previous = _load_previous(pathlib.Path(args.previous) if args.previous else out)

    result = detect(
        gsc,
        min_impressions=args.min_impressions,
        min_dominant_share=args.min_dominant_share,
        max_results=args.max_results,
        previous=previous,
        sticky_weeks=args.sticky_weeks,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fresh = sum(1 for r in result["detected"] if not r.get("carried_forward"))
    carried = len(result["detected"]) - fresh
    logger.info("wrote %s (%d classified pages: %d classified this week, %d carried forward)",
                out, len(result["detected"]), fresh, carried)
    return 0


if __name__ == "__main__":
    sys.exit(main())
