#!/usr/bin/env python3
"""classify_demand_topics.py

需要クエリを navi の対象範囲でトピック分類し、件数と impressions を出す (#2686/#3332 N2)。

なぜ必要か (2026-08-10 実測):
  PR #4861 で需要ギャップ検出の一次シグナルを omcha.jp (WP 本家) の GSC に移した。
  navi 自身の GSC は 30 日 2,202 impressions しかなく需要の定義に使えないためだが、
  WP 側 (30 日 112,205 impressions) は **navi より広い領域を扱っている**。
  imp>=50 の 451 クエリを実読すると、玩具 (トミカ収納・スクイーズ)、育児用品
  (授乳クッション・粉ミルク)、育児の悩み (ネントレ・泣き声)、明確な対象外
  (mbti 64タイプ診断・テトリス) が混在していた。

  ここで効くのは分類精度ではなく **navi が何を扱うサイトなのかが未決** という点。
  そこで本スクリプトは方針を決めずに「決めるための材料」= 各 bucket の件数と
  impressions を出すことに徹する。判断は owner が bucket の実数を見て行う。

  ブランドマスター (data/brand_taxonomy.yaml) は分類器に使えない。実測で WP 上位
  50 クエリのうちヒットは 5 件だけだった (「スクイーズ どこで売ってる」にブランド名は
  無く、「mbti 64」は弾けない)。あれは表記ゆれ正規化のためのマスターであって
  トピック分類器ではない。

判定順序 (data/demand_topic_terms.yaml の説明と同じ):
  out_of_scope → toy → baby_goods → parenting → unclassified

  - out_of_scope が最優先: mbti 等は他語と同居しても対象外
  - toy が baby_goods より先: 「赤ちゃん ハンドスピナー おすすめ」は玩具側に倒す
  - どれにも当たらないものは unclassified に残し、**pass に潰さない**。
    語彙を育てる対象として impressions 降順で出力する

使い方:
    python scripts/classify_demand_topics.py
    python scripts/classify_demand_topics.py --min-wp-impressions 50 --top-unclassified 40
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Any

import yaml

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import detect_demand_gaps as D  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("classify_demand_topics")

DEFAULT_TERMS_PATH = "data/demand_topic_terms.yaml"
DEFAULT_OUT = "data/analytics/demand_topics.json"

# 判定順序。先に当たった bucket を採用する (先勝ち)。
BUCKET_ORDER = ("out_of_scope", "toy", "baby_goods", "parenting")
UNCLASSIFIED = "unclassified"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: Any) -> str:
    """語・クエリ双方に同じ正規化をかける (NFKC + lower + 空白圧縮)。"""
    return D.normalize_query(text)


def load_terms(terms_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """YAML から bucket -> {"label": str, "terms": [正規化済み語]} を返す。

    BUCKET_ORDER に無い bucket が YAML にあれば警告して無視する (誤記の握り潰し
    防止)。逆に BUCKET_ORDER にあって YAML に無い bucket は空語彙として扱う。
    """
    data = yaml.safe_load(terms_path.read_text(encoding="utf-8"))
    buckets = (data or {}).get("buckets") or {}
    if not isinstance(buckets, dict):
        raise ValueError(f"{terms_path}: buckets はマッピングであること")

    for name in buckets:
        if name not in BUCKET_ORDER:
            logger.warning("unknown bucket %r in %s; ignored", name, terms_path)

    out: dict[str, dict[str, Any]] = {}
    for name in BUCKET_ORDER:
        spec = buckets.get(name) or {}
        raw_terms = spec.get("terms") or []
        terms = []
        for t in raw_terms:
            n = normalize(t)
            if n:
                terms.append(n)
        out[name] = {"label": spec.get("label") or name, "terms": terms}
    return out


def classify(query: str, terms: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
    """(bucket, 当たった語) を返す。どれにも当たらなければ (UNCLASSIFIED, None)。

    先勝ち。同一 bucket 内で複数語が当たった場合は **最も長い語**を根拠として
    返す (「トミカ」より「トミカ収納」の方が説明になる)。
    """
    q = normalize(query)
    for name in BUCKET_ORDER:
        matched = [t for t in terms[name]["terms"] if t in q]
        if matched:
            return name, max(matched, key=len)
    return UNCLASSIFIED, None


def build_report(
    demand_queries: list[dict[str, Any]],
    terms: dict[str, dict[str, Any]],
    top_unclassified: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for q in demand_queries:
        bucket, matched = classify(q["query"], terms)
        rows.append({
            "query": q["query"],
            "wp_impressions": q.get("wp_impressions", 0),
            "impressions": q.get("impressions", 0),
            "bucket": bucket,
            "matched_term": matched,
        })

    summary: dict[str, Any] = {}
    for name in list(BUCKET_ORDER) + [UNCLASSIFIED]:
        sel = [r for r in rows if r["bucket"] == name]
        summary[name] = {
            "label": terms[name]["label"] if name in terms else "未分類",
            "queries": len(sel),
            "wp_impressions": sum(r["wp_impressions"] for r in sel),
        }

    unclassified = sorted(
        (r for r in rows if r["bucket"] == UNCLASSIFIED),
        key=lambda r: -r["wp_impressions"],
    )
    return {
        "generated_at": _now_iso(),
        "summary": summary,
        "total_queries": len(rows),
        "total_wp_impressions": sum(r["wp_impressions"] for r in rows),
        "unclassified_top": unclassified[:top_unclassified],
        "rows": sorted(rows, key=lambda r: -r["wp_impressions"]),
    }


def run(
    terms_path: pathlib.Path,
    out_path: pathlib.Path,
    min_wp_impressions: int,
    top_unclassified: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    terms = load_terms(terms_path)
    demand = D.build_demand_queries(
        pathlib.Path(D.DEFAULT_SUGGEST_DIR),
        pathlib.Path(D.DEFAULT_GSC_QUERY_PATH),
        D.DEFAULT_MIN_IMPRESSIONS,
        gsc_wp_query_path=pathlib.Path(D.DEFAULT_GSC_WP_QUERY_PATH),
        min_wp_impressions=min_wp_impressions,
    )
    wp_demand = [q for q in demand if D.SOURCE_GSC_WP in q["sources"]]
    report = build_report(wp_demand, terms, top_unclassified)

    logger.info("WP 由来 需要クエリ %d 件 (wp_imp>=%d)", report["total_queries"], min_wp_impressions)
    for name in list(BUCKET_ORDER) + [UNCLASSIFIED]:
        s = report["summary"][name]
        logger.info("  %-13s %4d 件 / imp %8d  (%s)",
                    name, s["queries"], s["wp_impressions"], s["label"])

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("wrote %s", out_path)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("--terms", default=DEFAULT_TERMS_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--min-wp-impressions", type=int, default=D.DEFAULT_MIN_WP_IMPRESSIONS)
    ap.add_argument("--top-unclassified", type=int, default=40,
                    help="レポートに残す未分類クエリの件数 (語彙を育てる対象)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(pathlib.Path(args.terms), pathlib.Path(args.out),
        args.min_wp_impressions, args.top_unclassified, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
