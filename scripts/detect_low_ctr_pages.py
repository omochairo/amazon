"""detect_low_ctr_pages.py

GSC weekly JSON (scripts/fetch_gsc.py 出力) から CTR が site 平均を大きく
下回るページを抽出し、Jules リライト候補として `data/analytics/low_ctr_pages.json`
に書き出す read-only スクリプト。

検出条件 (A-1, epic #1356):
- position <= MAX_POSITION (default 10, page 1 のみ。position 11-20 は A-2 で別途扱う)
- impressions >= MIN_IMPRESSIONS (default 50, 統計的信号確保)
- ctr < max(baseline_ctr * RATIO, ABS_MIN_THRESHOLD)
  - baseline_ctr = totals.clicks_sitewide / totals.impressions_sitewide
    (真の site 全体 CTR。#3988 B-1 以前のスナップショットで *_sitewide が
    無い場合のみ totals.clicks_sum / totals.impressions_sum にフォールバック
    する。この2つは top-N ページだけの合計で truncated_pages=true のとき
    サイト全体より上振れするため、フォールバック時であることを出力 JSON の
    baseline_source で明示する)
  - RATIO default 1.5 (amazon-navi-brain#33: sitewide baseline に切り替えた際、直近8週の実デ
    ータで旧 baseline (top-N合計、RATIO=0.5) と同じ発火頻度になるようスイー
    プして再較正した値。1.0 を超えるため、ratio_to_baseline が 1 を超える
    = サイト平均以上のページも検出されうる。「検出された = サイト平均より
    低い」ではない点に注意 (Issue 本文の文言もこれを前提に書くこと)
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
DEFAULT_RATIO = 1.5
DEFAULT_ABS_MIN_THRESHOLD = 0.005
DEFAULT_MAX_RESULTS = 5
# 既知クエリ imp の下限 (amazon-navi-brain#128)
#
# by_combo (query×page) に出るクエリの imp 合計が小さいページは起票しない。GSC は
# 低ボリュームのクエリを匿名化して落とすので、ページの imp が閾値を越えていても
# 「どの語で出ているか」がほとんど見えないことがある。そういう issue は仕分けで
# 毎回「材料不足」「直す語が見えない」として作業なしで閉じられてきた。
#
# 既定値は brain#128 のスイープで決めた (実数は private)。by_combo の行から実際に
# title を直した既知の例が落ちない範囲で、それより下の「語がほぼ見えない」帯だけ
# 落とす値にしてある。割合 (ページ imp に対する比) ではなく絶対値にしたのは、
# 直すときの材料は「見えている語の量」で、大きいページほど割合が下がるため。
#
# by_combo が rowLimit に張り付いた週 (totals.truncated_combos) は、行の合計が
# 下限値でしかないので、この条件を当てない (落とす根拠にならない)。
DEFAULT_MIN_KNOWN_QUERY_IMP = 5
DEFAULT_TOP_QUERIES_PER_PAGE = 5


def compute_baseline_ctr(totals: dict[str, Any]) -> tuple[float, str]:
    """site 全体の CTR baseline と、その出どころ ("sitewide" | "fallback_topn") を返す。

    clicks_sitewide/impressions_sitewide (#3988 B-1) が真の site 全体集計。
    無い場合 (2026-07-26 以前のスナップショット) のみ clicks_sum/impressions_sum
    にフォールバックするが、これは top-N ページだけの合計で truncated_pages=true
    のとき上振れする (amazon-navi-brain#33)。impressions=0 なら 0 を返す。
    """
    impressions = totals.get("impressions_sitewide")
    clicks = totals.get("clicks_sitewide")
    if impressions:
        return clicks / impressions, "sitewide"
    impressions = totals.get("impressions_sum", 0) or 0
    clicks = totals.get("clicks_sum", 0) or 0
    if impressions <= 0:
        return 0.0, "fallback_topn"
    return clicks / impressions, "fallback_topn"


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


def sum_known_query_impressions(by_combo: list[dict]) -> dict[str, int]:
    """page URL -> by_combo に出ているクエリの impressions 合計。

    top_queries (上位 N 件) ではなく全行で数える。GSC が匿名化したクエリの分は
    by_page の impressions には入るがここには入らない。
    """
    known: dict[str, int] = {}
    for row in by_combo:
        page = row.get("page")
        if not page:
            continue
        known[page] = known.get(page, 0) + (row.get("impressions", 0) or 0)
    return known


def detect(gsc: dict[str, Any], *,
           min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
           max_position: float = DEFAULT_MAX_POSITION,
           ratio: float = DEFAULT_RATIO,
           abs_min_threshold: float = DEFAULT_ABS_MIN_THRESHOLD,
           max_results: int = DEFAULT_MAX_RESULTS,
           top_queries_per_page: int = DEFAULT_TOP_QUERIES_PER_PAGE,
           min_known_query_imp: int = DEFAULT_MIN_KNOWN_QUERY_IMP) -> dict[str, Any]:
    """GSC weekly JSON から低 CTR ページを抽出する。"""
    totals = gsc.get("totals", {})
    baseline, baseline_source = compute_baseline_ctr(totals)
    threshold = max(baseline * ratio, abs_min_threshold)
    logger.info("baseline_ctr=%.4f (source=%s), threshold=%.4f (ratio=%.2f, abs_min=%.4f)",
                baseline, baseline_source, threshold, ratio, abs_min_threshold)

    queries_by_page = collect_top_queries_by_page(gsc.get("by_combo", []), top_queries_per_page)
    known_by_page = sum_known_query_impressions(gsc.get("by_combo", []))
    # rowLimit に張り付いた週は行の合計が下限値でしかないので、既知 imp で落とさない。
    known_gate_applied = not totals.get("truncated_combos", False)
    if not known_gate_applied:
        logger.warning("by_combo is truncated (truncated_combos=true); "
                       "min_known_query_imp gate skipped this run")

    detected = []
    eligible = 0
    dropped_low_known = []
    for row in gsc.get("by_page", []):
        page = row.get("page")
        impressions = row.get("impressions", 0)
        position = row.get("position", 0.0)
        ctr = row.get("ctr", 0.0)
        if not page or impressions < min_impressions:
            continue
        eligible += 1
        if position <= 0 or position > max_position:
            continue
        if ctr >= threshold:
            continue
        known = known_by_page.get(page, 0)
        if known_gate_applied and known < min_known_query_imp:
            dropped_low_known.append(page)
            continue
        detected.append({
            "page": page,
            "clicks": row.get("clicks", 0),
            "impressions": impressions,
            "ctr": ctr,
            "position": position,
            "ratio_to_baseline": (ctr / baseline) if baseline > 0 else None,
            "known_query_impressions": known,
            "top_queries": queries_by_page.get(page, []),
        })

    # 上限で切る前に落とす。落としたページの枠を、材料のあるページが使えるようにする。
    detected.sort(key=lambda r: r["impressions"], reverse=True)
    logger.info("eligible=%d, matched=%d, dropped_low_known_query_imp=%d (min=%d, applied=%s)",
                eligible, len(detected) + len(dropped_low_known), len(dropped_low_known),
                min_known_query_imp, known_gate_applied)
    detected = detected[:max_results]

    return {
        "source_range": gsc.get("range"),
        "baseline_ctr": baseline,
        "baseline_source": baseline_source,
        "threshold_ctr": threshold,
        "params": {
            "min_impressions": min_impressions,
            "max_position": max_position,
            "ratio": ratio,
            "abs_min_threshold": abs_min_threshold,
            "max_results": max_results,
            "min_known_query_imp": min_known_query_imp,
        },
        "eligible": eligible,
        "known_query_gate_applied": known_gate_applied,
        "dropped_low_known_query_imp": len(dropped_low_known),
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
    p.add_argument("--min-known-query-imp", type=int, default=DEFAULT_MIN_KNOWN_QUERY_IMP,
                   help="by_combo に出るクエリの imp 合計がこれ未満のページは出さない (0 で無効)")
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
        min_known_query_imp=args.min_known_query_imp,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d detected, baseline_ctr=%.4f)",
                out, len(result["detected"]), result["baseline_ctr"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
