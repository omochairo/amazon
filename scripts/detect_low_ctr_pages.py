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

## 順位帯の期待 CTR とポアソン検定 (amazon-navi-brain#128 の続き)

上の「site 全体 CTR × RATIO」は順位を見ない。サイト自身の実績では CTR は順位で
大きく違い (1〜3 位と 7〜10 位で 2.5 倍前後)、一本の線だと上位のページに甘く、
9 位前後のページに厳しい。しかも imp 50 では期待クリックが 2 前後しかなく、
0 クリックは偶然でも普通に起きる。

そこで `main()` は `data/analytics/gsc_history/` の過去の週から順位帯ごとの CTR を
集め (判定する週そのものは除く)、このページの順位帯の CTR を期待値として
「この imp でこのクリック数以下になる確率」(ポアソン分布の下側確率) が ALPHA
未満のときだけ検出する。履歴の週をスイープすると、起票は約 3 分の 1 になり、
実際に title を直した既知の例 (brain#41) は残る (実数は brain#128)。

`detect()` に `band_ctr` を渡さないとき、または順位帯の imp が MIN_BAND_IMPRESSIONS
に満たないときは従来の site 全体 × RATIO に戻る。ライブラリとして `detect()` を
使う側 (audit_query_entailment など) の挙動は変えていない。

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
import math
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
DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
# 順位帯 (上限, ラベル)。上限は「以下」。max_position (10) を超える帯は持たない
POSITION_BANDS = ((3.0, "1-3"), (5.0, "3-5"), (7.0, "5-7"), (10.0, "7-10"))
# 下側確率がこれ未満なら「偶然では説明しにくいほどクリックが少ない」
DEFAULT_ALPHA = 0.05
# 帯の期待 CTR を信じるのに要る履歴の imp。満たない帯は従来の判定に戻る
MIN_BAND_IMPRESSIONS = 300


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


def position_band(position: float) -> str | None:
    """順位帯のラベル。0 以下・最後の帯の上限超えは None。"""
    if position <= 0:
        return None
    for upper, label in POSITION_BANDS:
        if position <= upper:
            return label
    return None


def _same_range(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and a.get("start") == b.get("start") and a.get("end") == b.get("end")


def load_band_ctr(history_dir: str | pathlib.Path, *,
                  exclude_range: dict | None = None) -> dict[str, dict[str, Any]]:
    """過去の週次スナップショットから順位帯ごとの clicks / impressions / ctr を集める。

    `exclude_range` と同じ range の週は数えない (判定する週を自分の期待値に混ぜない)。
    読めないファイルは飛ばす。どの帯にも行が無ければ空の dict。
    """
    sums: dict[str, list[int]] = {}
    for f in sorted(pathlib.Path(history_dir).glob("*.json")):
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("skip unreadable history file: %s", f)
            continue
        if _same_range(snap.get("range"), exclude_range):
            continue
        for row in snap.get("by_page", []):
            band = position_band(row.get("position", 0.0) or 0.0)
            if band is None:
                continue
            acc = sums.setdefault(band, [0, 0])
            acc[0] += row.get("clicks", 0) or 0
            acc[1] += row.get("impressions", 0) or 0
    return {
        band: {"clicks": c, "impressions": i, "ctr": c / i}
        for band, (c, i) in sums.items() if i > 0
    }


def poisson_lower_tail(k: int, lam: float) -> float:
    """P(X <= k), X ~ Poisson(lam)。

    各項は対数で作る。`exp(-lam)` から掛け上げると lam が 745 前後を超えた所で
    0 にアンダーフローし、クリック数が期待どおりでも確率 0 = 検出になってしまう。
    """
    if lam <= 0:
        return 1.0
    log_lam = math.log(lam)
    total = sum(math.exp(i * log_lam - lam - math.lgamma(i + 1)) for i in range(int(k) + 1))
    return min(total, 1.0)


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
           min_known_query_imp: int = DEFAULT_MIN_KNOWN_QUERY_IMP,
           band_ctr: dict[str, dict[str, Any]] | None = None,
           alpha: float = DEFAULT_ALPHA) -> dict[str, Any]:
    """GSC weekly JSON から低 CTR ページを抽出する。

    `band_ctr` (load_band_ctr の戻り値) を渡すと、履歴が足りる順位帯では
    順位帯の期待 CTR + ポアソン検定で判定する。渡さなければ従来どおり。
    """
    totals = gsc.get("totals", {})
    baseline, baseline_source = compute_baseline_ctr(totals)
    threshold = max(baseline * ratio, abs_min_threshold)
    logger.info("baseline_ctr=%.4f (source=%s), threshold=%.4f (ratio=%.2f, abs_min=%.4f)",
                baseline, baseline_source, threshold, ratio, abs_min_threshold)
    usable_bands = {
        band: v for band, v in (band_ctr or {}).items()
        if v.get("impressions", 0) >= MIN_BAND_IMPRESSIONS
    }
    if band_ctr is not None:
        logger.info("position-band expected CTR: %s (alpha=%.3f, min_band_imp=%d)",
                    {b: round(v["ctr"], 4) for b, v in usable_bands.items()},
                    alpha, MIN_BAND_IMPRESSIONS)

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
        band = position_band(position)
        expected = usable_bands.get(band) if band else None
        p_value = None
        if expected is not None:
            p_value = poisson_lower_tail(row.get("clicks", 0) or 0,
                                         impressions * expected["ctr"])
            if p_value >= alpha:
                continue
        elif ctr >= threshold:
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
            "rule": "position_poisson" if expected is not None else "sitewide_ratio",
            "position_band": band,
            "expected_ctr": expected["ctr"] if expected is not None else None,
            "p_value": p_value,
            "known_query_impressions": known,
            "top_queries": queries_by_page.get(page, []),
        })

    # 上限で切る前に落とす。落としたページの枠を、材料のあるページが使えるようにする。
    # 検定したものは確率の小さい (偶然で説明しにくい) 順、従来判定のものはその後ろに imp 順。
    detected.sort(key=lambda r: (r["p_value"] is None, r["p_value"] or 0.0,
                                 -r["impressions"]))
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
            "alpha": alpha,
            "min_band_impressions": MIN_BAND_IMPRESSIONS,
        },
        "band_ctr": usable_bands,
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
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR,
                   help="順位帯の期待 CTR を集める週次スナップショットの置き場。"
                        "空文字で従来の site 全体 × ratio 判定に戻す")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    gsc = json.loads(in_path.read_text(encoding="utf-8"))
    band_ctr = None
    if args.history_dir and pathlib.Path(args.history_dir).is_dir():
        band_ctr = load_band_ctr(args.history_dir, exclude_range=gsc.get("range"))
    elif args.history_dir:
        logger.warning("history dir not found: %s — falling back to sitewide ratio",
                       args.history_dir)

    result = detect(
        gsc,
        min_impressions=args.min_impressions,
        max_position=args.max_position,
        ratio=args.ratio,
        abs_min_threshold=args.abs_min_threshold,
        max_results=args.max_results,
        min_known_query_imp=args.min_known_query_imp,
        band_ctr=band_ctr,
        alpha=args.alpha,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d detected, baseline_ctr=%.4f)",
                out, len(result["detected"]), result["baseline_ctr"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
