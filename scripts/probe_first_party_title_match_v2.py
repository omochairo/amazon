"""probe_first_party_title_match_v2.py

#7569 型6 の probe (A) 「記事タイトルと商品名の一致」を、リークの無い評価セットで測り直す。

なぜ必要か:
  #7601 の「全負例を落とす閾値で正例 9/12 (75%) が残る」は、ラベルの付け方
  (母艦が post_title を読んで判定) と (A) が測る特徴量 (商品名が post_title に出るか)
  がほぼ同じ操作になっているリークで膨らんでいた (#7569 コメント5)。正例を出自で
  割ると、post_title を読まずに本文だけで採用した 7 件は 4/7 まで落ち、題名を読んで
  「その本のレビュー記事」と判断した 5 件は 5/5 という完全な循環だった。

  #7610 で **本文のみから** 判定した評価セット
  (`data/analytics/first_party_eval_labels.json`) が版として固定されたので、
  同じ計測ロジックをこのラベルで測り直す (#7569 コメント6)。

設計判断:
  - **これは計測であって実装ではない**。本番の選別ロジック (`determine_roles` /
    `PRIMARY_SHARE_FLOOR` / `build_first_party_pool`) には一切配線しない
  - 商品名候補の抽出・正規化・指標 (`lcs_len` / `lcs_ratio` / `bigram_jaccard`) は
    `probe_first_party_title_match` から import する (複製しない・調整しない)
  - ラベルは `items[].label` を使う。`review` を正例、`mention` を負例とする。
    `unclear` (1 件) は母集団から除外する
  - `items[].prior_title_label` は読まない (#7601 が使っていた旧ラベル。混ぜない)
  - `POSITIVE_ASINS` / `NEGATIVE_ASINS` (#7601 の題名ラベル定数) は使わない
  - 本文取得は #7592/#7601 が残した /tmp キャッシュをそのまま使う
    (`fetch_contents_cached`)。キャッシュに無い ASIN があれば GET するだけで、
    書き込み系リクエストは一切出さない
  - `data/` には何も書かない・コミットしない。既定の出力先は /tmp 配下
  - しきい値は提案しない。分布と「全正例を残すときに負例を何件落とせるか」
    「全負例を落とすときに正例が何件残るか」の 2 点、source 別内訳だけを出す

使い方:
    python -m scripts.probe_first_party_title_match_v2 --limit 5   # スモーク
    python -m scripts.probe_first_party_title_match_v2              # フル実行 (47件)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time as time_module
from typing import Any

import requests

from scripts.build_wp_navi_link_candidates import DEFAULT_SLEEP_SECONDS, DEFAULT_WP_BASE_URL
from scripts.collect_first_party_sources import fetch_post_content
from scripts.probe_first_party_link_placement import (
    DEFAULT_CACHE,
    fetch_contents_cached,
    load_cache,
    save_cache,
)
from scripts.probe_first_party_link_proximity import (
    _now_iso,
    build_link_to_post_id,
    distribution_stats,
    full_drop_specificity,
    full_recall_specificity,
)
from scripts.probe_first_party_title_match import (
    METRICS,
    best_scores_for_asin,
    build_post_titles,
    extract_product_name_candidates,
    match_scores,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_first_party_title_match_v2")

DEFAULT_SOURCES_PATH = "data/analytics/first_party_sources.json"
DEFAULT_LABELS_PATH = "data/analytics/first_party_eval_labels.json"
DEFAULT_OUT = "/tmp/first_party_title_match_v2_probe.json"
# #7592/#7601 が残した /tmp キャッシュをそのまま再利用する (再取得不要)
DEFAULT_TITLE_MATCH_CACHE = DEFAULT_CACHE

LABEL_POSITIVE = "review"
LABEL_NEGATIVE = "mention"
OUTCOME_BY_LABEL: dict[str, str] = {LABEL_POSITIVE: "positive", LABEL_NEGATIVE: "negative"}

SOURCE_CALIBRATION = "calibration_positive"
SOURCE_POOL = "pool_random_sample"


# --------------------------------------------------------------------------
# ラベル読み込み (pure functions。ネットワークを叩かない)
# --------------------------------------------------------------------------

def load_labels_payload(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def labeled_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """review/mention のみを返す (unclear を除外)。prior_title_label は読まない。"""
    out: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        label = item.get("label")
        outcome = OUTCOME_BY_LABEL.get(label)
        if outcome is None:
            continue
        out.append({
            "asin": item["asin"],
            "post_url": item["post_url"],
            "label": label,
            "outcome": outcome,
            "source": item.get("source"),
        })
    return out


def positive_asins(items: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted(i["asin"] for i in items if i["outcome"] == "positive"))


def negative_asins(items: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted(i["asin"] for i in items if i["outcome"] == "negative"))


# --------------------------------------------------------------------------
# post の引き当て ((asin, post_url) -> post_id / post_title)
# --------------------------------------------------------------------------

def resolve_items(
    items: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    posts_cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """ラベル項目に post_id / post_title を付与する。

    引き当てられなかった ASIN は missing_post_id (posts_cache に post_url が無い) /
    missing_post_title (sources に (asin, post_url) の post_title が無い) に分けて返す。
    後者は post_id があれば resolved にも残す (取得・候補抽出自体は続けられるため)。
    """
    post_titles = build_post_titles(sources)
    link_to_id = build_link_to_post_id(posts_cache)
    resolved: list[dict[str, Any]] = []
    missing_post_id: list[str] = []
    missing_post_title: list[str] = []
    for item in items:
        asin, url = item["asin"], item["post_url"]
        pid = link_to_id.get(url)
        title = post_titles.get((asin, url))
        if pid is None:
            missing_post_id.append(asin)
            continue
        if title is None:
            missing_post_title.append(asin)
        resolved.append({**item, "post_id": pid, "post_title": title})
    return resolved, missing_post_id, missing_post_title


# --------------------------------------------------------------------------
# 測定・集計 (商品名候補抽出・指標そのものは v1 の import を使うだけ)
# --------------------------------------------------------------------------

def build_pairs(resolved_items: list[dict[str, Any]], contents: dict[str, str]) -> list[dict[str, Any]]:
    pairs = []
    for item in resolved_items:
        content = contents.get(item["post_id"])
        if content is None:
            pairs.append({**item, "candidates": None, "candidate_scores": None, "fetch_failed": True})
            continue
        candidates = extract_product_name_candidates(content, item["asin"])
        candidate_scores = (
            [match_scores(c, item["post_title"]) for c in candidates] if item["post_title"] else []
        )
        pairs.append({
            **item, "candidates": candidates, "candidate_scores": candidate_scores, "fetch_failed": False,
        })
    return pairs


def aggregate_by_asin(pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """asin -> 指標ごとの最大値 + 診断用フラグ。ラベル項目は 1 asin = 1 post なので畳み込みは不要。"""
    out: dict[str, dict[str, Any]] = {}
    for p in pairs:
        scores = best_scores_for_asin(p["candidate_scores"]) if p["candidate_scores"] else None
        out[p["asin"]] = {
            "post_id": p["post_id"],
            "outcome": p["outcome"],
            "source": p["source"],
            "fetch_failed": p["fetch_failed"],
            "missing_post_title": p["post_title"] is None,
            "no_candidate": (
                not p["fetch_failed"] and p["post_title"] is not None and not p["candidates"]
            ),
            "scores": scores,
        }
    return out


def subset_kept_at_ceiling(
    agg: dict[str, dict[str, Any]], asins: tuple[str, ...], metric: str, ceiling: float | None,
) -> dict[str, Any] | None:
    """全負例を落とす閾値 (ceiling) のとき、asins のうち何件が残るか。"""
    if ceiling is None:
        return None
    values = [agg[a]["scores"][metric] for a in asins if agg[a]["scores"] is not None]
    return {"n": len(values), "kept": sum(1 for v in values if v > ceiling)}


def metric_report(
    agg: dict[str, dict[str, Any]], pos_asins: tuple[str, ...], neg_asins: tuple[str, ...], metric: str,
) -> dict[str, Any]:
    pos_values = [agg[a]["scores"][metric] for a in pos_asins if agg[a]["scores"] is not None]
    neg_values = [agg[a]["scores"][metric] for a in neg_asins if agg[a]["scores"] is not None]
    return {
        "positive": distribution_stats(pos_values),
        "negative": distribution_stats(neg_values),
        "full_recall_specificity": full_recall_specificity(pos_values, neg_values),
        "full_drop_specificity": full_drop_specificity(pos_values, neg_values),
        "positive_missing": len(pos_asins) - len(pos_values),
        "negative_missing": len(neg_asins) - len(neg_values),
    }


def build_report(
    *,
    agg: dict[str, dict[str, Any]],
    resolved_items: list[dict[str, Any]],
    pos_asins: tuple[str, ...],
    neg_asins: tuple[str, ...],
    missing_post_id: list[str],
    missing_post_title: list[str],
    fetch_failed_post_ids: list[str],
    cache_miss_asins: list[str],
    total_pairs: int,
    labels_path: pathlib.Path,
) -> dict[str, Any]:
    items_by_asin = {i["asin"]: i for i in resolved_items}
    metrics: dict[str, Any] = {}
    for metric in METRICS:
        m = metric_report(agg, pos_asins, neg_asins, metric)
        drop = m["full_drop_specificity"]
        ceiling = drop["neg_ceiling"] if drop else None
        calibration_asins = tuple(
            a for a in pos_asins if items_by_asin.get(a, {}).get("source") == SOURCE_CALIBRATION
        )
        pool_positive_asins = tuple(
            a for a in pos_asins if items_by_asin.get(a, {}).get("source") == SOURCE_POOL
        )
        m["source_breakdown"] = {
            SOURCE_CALIBRATION: subset_kept_at_ceiling(agg, calibration_asins, metric, ceiling),
            f"{SOURCE_POOL}_positive": subset_kept_at_ceiling(agg, pool_positive_asins, metric, ceiling),
        }
        metrics[metric] = m

    no_candidate_pairs = sum(1 for row in agg.values() if row["no_candidate"])

    return {
        "generated_at": _now_iso(),
        "labels_path": str(labels_path),
        "positive_asins": list(pos_asins),
        "negative_asins": list(neg_asins),
        "missing_post_id_asins": missing_post_id,
        "missing_post_title_asins": missing_post_title,
        "fetch_failed_posts": fetch_failed_post_ids,
        "cache_miss_asins": cache_miss_asins,
        "total_pairs": total_pairs,
        "no_candidate_pairs": no_candidate_pairs,
        "metrics": metrics,
        "per_asin": agg,
    }


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def run(
    *,
    sources_path: pathlib.Path,
    labels_path: pathlib.Path,
    out_path: pathlib.Path,
    cache_path: pathlib.Path,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    session: requests.Session | None = None,
    sleeper=None,
    fetch_fn=fetch_post_content,
) -> dict[str, Any]:
    sleeper = sleeper or time_module.sleep
    session = session or requests.Session()

    labels_payload = load_labels_payload(labels_path)
    items = labeled_items(labels_payload)
    if limit and limit > 0:
        items = items[:limit]

    sources_payload = json.loads(sources_path.read_text(encoding="utf-8"))
    sources = sources_payload.get("sources") or []
    posts_cache = sources_payload.get("posts_cache") or {}

    resolved, missing_post_id, missing_post_title = resolve_items(items, sources, posts_cache)

    unique_ids = sorted({i["post_id"] for i in resolved}, key=int)
    logger.info(
        "対象 post: %d 件 (正例%d + 負例%d ASIN、post 引き当て失敗 %d 件)",
        len(unique_ids), len(positive_asins(items)), len(negative_asins(items)), len(missing_post_id),
    )

    cache = load_cache(cache_path)
    cache_miss_ids = {pid for pid in unique_ids if pid not in cache}
    cache_miss_asins = sorted({i["asin"] for i in resolved if i["post_id"] in cache_miss_ids})

    contents, failed_ids = fetch_contents_cached(
        unique_ids, wp_base_url, session, sleep_seconds, 0, cache, sleeper=sleeper, fetch_fn=fetch_fn,
    )
    save_cache(cache_path, cache)
    if failed_ids:
        logger.warning("本文取得に失敗した post: %d 件 %s", len(failed_ids), failed_ids)
    if cache_miss_asins:
        logger.warning("キャッシュに無かった ASIN (新規取得): %d 件 %s", len(cache_miss_asins), cache_miss_asins)

    pairs = build_pairs(resolved, contents)
    agg = aggregate_by_asin(pairs)

    pos_resolved = tuple(a for a in positive_asins(items) if a in agg)
    neg_resolved = tuple(a for a in negative_asins(items) if a in agg)

    report = build_report(
        agg=agg,
        resolved_items=resolved,
        pos_asins=pos_resolved,
        neg_asins=neg_resolved,
        missing_post_id=missing_post_id,
        missing_post_title=missing_post_title,
        fetch_failed_post_ids=failed_ids,
        cache_miss_asins=cache_miss_asins,
        total_pairs=len(pairs),
        labels_path=labels_path,
    )

    write_json(out_path, report)
    logger.info(
        "wrote %s (pairs=%d, no_candidate=%d, missing_post_id=%d, missing_post_title=%d, "
        "fetch_failed=%d, cache_miss=%d)",
        out_path, len(pairs), report["no_candidate_pairs"], len(missing_post_id),
        len(missing_post_title), len(failed_ids), len(cache_miss_asins),
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=DEFAULT_SOURCES_PATH)
    ap.add_argument("--labels", default=DEFAULT_LABELS_PATH, help="本文ラベル評価セット (#7610)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=DEFAULT_TITLE_MATCH_CACHE, help="本文取得結果の /tmp キャッシュ")
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    ap.add_argument("--limit", type=int, default=0, help="対象ラベル項目数の上限 (スモーク用, 0=無制限)")
    args = ap.parse_args()
    run(
        sources_path=pathlib.Path(args.sources),
        labels_path=pathlib.Path(args.labels),
        out_path=pathlib.Path(args.out),
        cache_path=pathlib.Path(args.cache),
        wp_base_url=args.wp_base_url,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
