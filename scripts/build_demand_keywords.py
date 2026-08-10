#!/usr/bin/env python3
"""build_demand_keywords.py

WP の実需要クエリを Amazon SearchItems 用の検索キーワードに変換する (#2686)。

なぜ必要か (2026-08-10 実測):
  fetch_amazon.py の DEFAULT_KEYWORDS は 231 件の**供給側**リスト (ブランド名・
  年齢 × カテゴリ・素材) で、「どんなおもちゃが存在するか」から引いている。
  実測では navi は 554 記事中 417 (75%) が平均 10 位以内で**順位は取れている**
  のに、サイト全体で 376 impressions/日 しかない。誰も検索しない語で 1 位を
  取っている状態で、供給駆動の構造的な帰結。

  omcha.jp (WP 本家) の GSC には同領域で 51 倍の実需要があり、toy bucket だけで
  237,885 impressions ある (#4862 の分類レポート)。本スクリプトはそれを
  fetch_amazon.py --keywords に渡せる形に変換する。

変換の要点:
  需要クエリは情報型 (「スクイーズ どこで買える」「トミカ 収納 100均」) なので、
  そのまま SearchItems に投げても商品が返らない。data/demand_topic_terms.yaml の
  search_modifiers (買い方・評判・出来事・販売店を表す語) を落とし、商品の実体を
  指す語だけ残す。**商品カテゴリ語は落とさない** —「トミカ 収納」の「収納」を
  落とすとミニカー本体が返り、検索意図と違う商品を拾う。

供給ゲート (excluded_keywords):
  需要があっても Amazon に商品が無ければ navi の記事型 (商品ページ) にならない。
  実例としてメロジョイ (スクイーズ玩具・WP 需要 約 110,000 imp = 最大クラスタ)
  は Amazon で販売されておらず、SearchItems に投げても 0 件で API 呼び出しを
  捨てるだけになる。除外は理由つきで YAML に書き、握り潰さない。

  ここで落とせるのは「事前に分かっている」分だけである点に注意。残りの供給有無は
  実際に SearchItems を叩くまで分からない。ヒット 0 件の記録は配線側 (後続) の仕事。

本スクリプトは **inert** (外部 API を呼ばず cron にも繋がない)。出力を見てから
fetch_amazon.py への配線を決める。

使い方:
    python scripts/build_demand_keywords.py
    python scripts/build_demand_keywords.py --buckets toy,baby_goods --limit 100
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_demand_keywords")

DEFAULT_TERMS_PATH = "data/demand_topic_terms.yaml"
DEFAULT_TOPICS_PATH = "data/analytics/demand_topics.json"
DEFAULT_OUT = "data/demand_keywords.json"
DEFAULT_BUCKETS = ("toy",)
# SearchItems に投げる意味のある最短長。1 文字の残骸を弾く。
MIN_KEYWORD_CHARS = 2

_SPACE_RE = re.compile(r"[\s　]+")
# 末尾に残る助詞 1 文字 (修飾語を落とした残骸)。「…1000と1500の」→「…1000と1500」
_TRAILING_PARTICLE_RE = re.compile(r"[のとがはをにでもへや]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: Any) -> str:
    if not text:
        return ""
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(text)).lower()).strip()


def normalize_key(text: Any) -> str:
    """空白を**完全に除去**した正規化キー。

    日本語クエリで空白は意味を持たないうえ、GSC のクエリは Google 側の分かち書きが
    そのまま入っていて揺れる (「はじめて ず かん 1000」「ぷにる ん ず」「危険 性」)。
    空白を残したまま扱うと (1) 修飾語が分断されて落ちない (「危険 性」に「危険性」が
    当たらない)、(2) 同一語が別集計になる (「トミカ 収納」21,365 と「トミカ収納」
    8,056) の 2 つを同時に踏む。2026-08-10 に実データで両方踏んで空白除去に変えた。
    """
    return _SPACE_RE.sub("", normalize(text))


def load_vocab(terms_path: pathlib.Path) -> tuple[list[str], list[dict[str, Any]]]:
    """(search_modifiers, excluded_keywords) を返す。

    modifiers は**長い順**に並べて返す。「どこで売ってる」を先に落とさないと
    「売ってる」だけ消えて「どこで」が残るため。
    """
    data = yaml.safe_load(terms_path.read_text(encoding="utf-8")) or {}
    mods = [normalize_key(m) for m in (data.get("search_modifiers") or [])]
    mods = sorted({m for m in mods if m}, key=len, reverse=True)
    excluded = data.get("excluded_keywords") or []
    return mods, excluded


def build_excluded_terms(excluded: list[dict[str, Any]]) -> dict[str, str]:
    """正規化済みの除外語 -> 理由 の辞書。keyword 本体と aliases の両方を張る。"""
    out: dict[str, str] = {}
    for e in excluded:
        if not isinstance(e, dict):
            continue
        reason = (e.get("reason") or "").strip()
        for name in [e.get("keyword")] + list(e.get("aliases") or []):
            n = normalize_key(name)
            if n:
                out[n] = reason
    return out


def to_search_keyword(query: str, modifiers: list[str]) -> str:
    """需要クエリから修飾語を落として検索語にする。

    空白を除去した正規化キー上で処理する (normalize_key の docstring 参照)。
    modifiers は長い順に適用される前提 (load_vocab がそう並べる)。記号と、修飾語を
    落とした残骸の末尾助詞を掃除し、短すぎるものは空文字を返して呼び出し側で捨てる。
    """
    s = normalize_key(query)
    for m in modifiers:
        if m in s:
            s = s.replace(m, "")
    s = re.sub(r"[)(（）・:：\-–—…!！?？'\"”“'’+&/／,、]+", "", s)
    s = _TRAILING_PARTICLE_RE.sub("", s).strip()
    if len(s) < MIN_KEYWORD_CHARS:
        return ""
    return s


def build(
    topics: dict[str, Any],
    modifiers: list[str],
    excluded_terms: dict[str, str],
    buckets: tuple[str, ...],
    limit: int = 0,
) -> dict[str, Any]:
    """検索語ごとに需要 impressions を合算したランキングを返す。

    同じ検索語に落ちる需要クエリ (「メロジョイ 偽物」「メロジョイ 定価」) は
    1 件にまとめ、impressions を合算する。合算するのは**同一サイト内の同一語**
    なので意味が通る (別サイトの合算をしないのとは別の話)。
    """
    agg: dict[str, dict[str, Any]] = {}
    skipped_excluded: dict[str, dict[str, Any]] = {}
    skipped_empty = 0

    for row in topics.get("rows") or []:
        if row.get("bucket") not in buckets:
            continue
        query = row.get("query") or ""
        imp = row.get("wp_impressions") or 0

        hit = next((t for t in excluded_terms if t in normalize_key(query)), None)
        if hit:
            e = skipped_excluded.setdefault(
                hit, {"term": hit, "reason": excluded_terms[hit], "queries": 0, "wp_impressions": 0})
            e["queries"] += 1
            e["wp_impressions"] += imp
            continue

        kw = to_search_keyword(query, modifiers)
        if not kw:
            skipped_empty += 1
            continue

        entry = agg.setdefault(kw, {"keyword": kw, "wp_impressions": 0, "source_queries": []})
        entry["wp_impressions"] += imp
        entry["source_queries"].append(query)

    keywords = sorted(agg.values(), key=lambda e: (-e["wp_impressions"], e["keyword"]))
    if limit > 0:
        keywords = keywords[:limit]

    return {
        "generated_at": _now_iso(),
        "params": {"buckets": list(buckets), "limit": limit},
        "summary": {
            "keywords": len(keywords),
            "wp_impressions": sum(k["wp_impressions"] for k in keywords),
            "excluded_terms": len(skipped_excluded),
            "excluded_wp_impressions": sum(e["wp_impressions"] for e in skipped_excluded.values()),
            "dropped_empty_after_strip": skipped_empty,
        },
        "excluded": sorted(skipped_excluded.values(), key=lambda e: -e["wp_impressions"]),
        "keywords": keywords,
    }


def run(terms_path: pathlib.Path, topics_path: pathlib.Path, out_path: pathlib.Path,
        buckets: tuple[str, ...], limit: int, dry_run: bool = False) -> dict[str, Any]:
    modifiers, excluded = load_vocab(terms_path)
    topics = json.loads(topics_path.read_text(encoding="utf-8"))
    result = build(topics, modifiers, build_excluded_terms(excluded), buckets, limit)

    s = result["summary"]
    logger.info("検索キーワード %d 件 / 需要 %d imp (buckets=%s)",
                s["keywords"], s["wp_impressions"], ",".join(buckets))
    logger.info("  除外 %d 語 / %d imp (Amazon 非販売など)",
                s["excluded_terms"], s["excluded_wp_impressions"])
    logger.info("  修飾語を落として空になった需要クエリ: %d 件", s["dropped_empty_after_strip"])

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        logger.info("wrote %s", out_path)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="需要クエリを Amazon 検索キーワードに変換する (#2686)")
    ap.add_argument("--terms", default=DEFAULT_TERMS_PATH)
    ap.add_argument("--topics", default=DEFAULT_TOPICS_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--buckets", default=",".join(DEFAULT_BUCKETS),
                    help="対象 bucket の CSV (既定 toy)。範囲決定が変わったらここを変える")
    ap.add_argument("--limit", type=int, default=0, help="出力する検索語の上限 (0=全件)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    buckets = tuple(b.strip() for b in args.buckets.split(",") if b.strip())
    run(pathlib.Path(args.terms), pathlib.Path(args.topics), pathlib.Path(args.out),
        buckets, args.limit, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
