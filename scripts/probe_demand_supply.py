#!/usr/bin/env python3
"""probe_demand_supply.py

需要キーワードに Amazon の商品供給があるかを **観測だけ** する shadow レーン (#2686)。

なぜ必要か:
  #4863 で WP の実需要を Amazon 検索キーワード 98 語に変換した (toy bucket /
  107,509 imp)。事前に分かっている非販売品 (メロジョイ = owner 確認・約 111,000 imp)
  と実店舗名は除外済みだが、**残りの供給有無は SearchItems を叩くまで分からない**。
  需要があっても商品が無ければ navi の記事型 (Amazon 商品ページ) にならないので、
  本番のキーワードプールを触る前にここを実測する。

設計判断:
  - **観測のみ**。data/raw/amazon.json を書かない = Jules の生成プールに一切
    入らない。記事は 1 本も作らない (quality census / uniqueness 監査と同じ規律)
  - 1 keyword あたり SearchItems 1 回だけ。fetch_amazon と同じ 1.1 秒間隔を空ける
  - 既存記事に無い ASIN を new_asins として数える。「供給はあるが全部既出」と
    「そもそも供給が無い」を区別するため (どちらも記事化できない点は同じだが、
    打ち手が違う)
  - API 失敗は keyword 単位で記録して次へ進む。1 語の失敗で全体を落とさない
    (ただし全語失敗は認証事故なので summary の error_count で分かる)

使い方:
    python scripts/probe_demand_supply.py --limit 5 --dry-run   # API を呼ばない
    python scripts/probe_demand_supply.py                       # secrets が要る
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_demand_supply")

DEFAULT_KEYWORDS_PATH = "data/demand_keywords.json"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "data/analytics/demand_supply_probe.json"
DEFAULT_SEARCH_INDEX = "Toys"
DEFAULT_ITEM_COUNT = 10
SLEEP_SECONDS = 1.1
# これ以上の語数で「全語 0 件・エラーなし」なら実装バグを疑う (summarize 参照)。
SUSPICIOUS_MIN_KEYWORDS = 10

_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(B0[A-Z0-9]{8})$")
_SIDECAR = (".enrichment", ".seo", ".quality")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_existing_asins(articles_dir: pathlib.Path) -> set[str]:
    """既に記事がある ASIN の集合 (slug から取る。JSON を開かないので速い)。"""
    out: set[str] = set()
    if not articles_dir.is_dir():
        return out
    for p in articles_dir.glob("*.json"):
        if p.stem.endswith(_SIDECAR):
            continue
        m = _SLUG_RE.match(p.stem)
        if m:
            out.add(m.group(1))
    return out


def extract_asins(response: Any) -> list[str]:
    """SearchItems レスポンスから ASIN を順序どおり取り出す。

    Creators API の SearchItems は **searchResult.items** に返す
    (fetch_amazon.py も `_safe_get(res, "searchResult", "items")` で読んでいる)。
    2026-08-10 の初版はトップレベルの ``items`` を見ており、98 語すべてが
    「hits=0 / error なし」= あたかも Amazon に商品が無いかのように見えていた。
    FakeAPI のテストを自分の誤った構造で書いたので単体テストは通っていた。
    実レスポンス構造の回帰テスト (test_real_search_response_shape) を必ず残すこと。

    形が違う/空のときは空リストを返す (例外にしない)。
    """
    if not isinstance(response, dict):
        return []
    items = response.get("searchResult", {})
    items = items.get("items") if isinstance(items, dict) else None
    if not isinstance(items, list):
        # 念のためトップレベルも見る (クライアント側で平坦化された場合)
        items = response.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            asin = it.get("asin")
            if isinstance(asin, str) and asin:
                out.append(asin)
    return out


def probe_keyword(api, keyword: str, existing: set[str], search_index: str,
                  item_count: int) -> dict[str, Any]:
    """1 keyword を検索して結果を要約する。API 例外は error として畳んで返す。"""
    try:
        res = api.search_items(keywords=keyword, search_index=search_index,
                               item_count=item_count, item_page=1)
    except Exception as e:  # API 側の例外型に依存しない (1 語の失敗で全体を止めない)
        return {"keyword": keyword, "error": f"{type(e).__name__}: {e}",
                "hits": 0, "new_asins": [], "known_asins": []}
    asins = extract_asins(res)
    return {
        "keyword": keyword,
        "error": None,
        "hits": len(asins),
        "new_asins": [a for a in asins if a not in existing],
        "known_asins": [a for a in asins if a in existing],
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    zero = [r for r in results if not r["error"] and r["hits"] == 0]
    no_new = [r for r in results if not r["error"] and r["hits"] > 0 and not r["new_asins"]]
    usable = [r for r in results if not r["error"] and r["new_asins"]]
    errors = [r for r in results if r["error"]]
    probed = len(results)
    return {
        "keywords_probed": probed,
        "zero_hit": len(zero),
        "hits_but_all_known": len(no_new),
        "usable": len(usable),
        "error_count": len(errors),
        "new_asin_total": len({a for r in usable for a in r["new_asins"]}),
        # 「全語 0 件かつエラーなし」は供給の実態ではなくレスポンス解釈のバグを疑う。
        # 2026-08-10 に実際に踏んだ (searchResult.items を items と誤読して 98/98 が 0)。
        # 観測レーンが誤った結論 (「需要語に商品が無い」) を静かに出さないための番人。
        "suspicious_all_zero": probed >= SUSPICIOUS_MIN_KEYWORDS and len(zero) == probed
                               and not errors,
    }


def run(keywords_path: pathlib.Path, articles_dir: pathlib.Path, out_path: pathlib.Path,
        limit: int, search_index: str, item_count: int, dry_run: bool,
        api=None, sleeper=time.sleep) -> dict[str, Any]:
    payload = json.loads(keywords_path.read_text(encoding="utf-8"))
    entries = payload.get("keywords") or []
    if limit > 0:
        entries = entries[:limit]
    existing = load_existing_asins(articles_dir)
    logger.info("既存記事 ASIN: %d 件 / 需要キーワード: %d 語", len(existing), len(entries))

    if dry_run:
        logger.info("[dry-run] API を呼ばずに終了する。対象語: %s",
                    ", ".join(e["keyword"] for e in entries[:5]))
        return {"summary": {"keywords_probed": 0}, "results": []}

    if api is None:
        from creators_api_client import CreatorsAPIClient  # 遅延 import (dry-run では不要)
        api = CreatorsAPIClient()

    results: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        r = probe_keyword(api, e["keyword"], existing, search_index, item_count)
        r["wp_impressions"] = e.get("wp_impressions", 0)
        results.append(r)
        logger.info("  [%3d/%3d] %-24s hits=%2d new=%2d %s",
                    i + 1, len(entries), e["keyword"][:24], r["hits"],
                    len(r["new_asins"]), r["error"] or "")
        if i + 1 < len(entries):
            sleeper(SLEEP_SECONDS)

    summary = summarize(results)
    # 需要の大きい順に並べ替えて出す (打ち手の優先順位がそのまま読める)
    results.sort(key=lambda r: -r.get("wp_impressions", 0))
    report = {"generated_at": _now_iso(),
              "params": {"search_index": search_index, "item_count": item_count, "limit": limit},
              "summary": summary, "results": results}

    logger.info("供給あり(新規ASINあり) %d 語 / 既出のみ %d 語 / 0 件 %d 語 / エラー %d 語",
                summary["usable"], summary["hits_but_all_known"],
                summary["zero_hit"], summary["error_count"])
    if summary["suspicious_all_zero"]:
        logger.error("全 %d 語が 0 件でエラーも無い。供給の実態ではなくレスポンス解釈の"
                     "バグを疑うこと (searchResult.items の読み違いを 2026-08-10 に踏んだ)",
                     summary["keywords_probed"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out_path)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="需要キーワードの Amazon 供給有無を観測する (#2686)")
    ap.add_argument("--keywords", default=DEFAULT_KEYWORDS_PATH)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="上位 N 語だけ叩く (0=全件)")
    ap.add_argument("--search-index", default=DEFAULT_SEARCH_INDEX)
    ap.add_argument("--item-count", type=int, default=DEFAULT_ITEM_COUNT)
    ap.add_argument("--dry-run", action="store_true", help="API を一切呼ばない")
    args = ap.parse_args()
    run(pathlib.Path(args.keywords), pathlib.Path(args.articles_dir), pathlib.Path(args.out),
        args.limit, args.search_index, args.item_count, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
