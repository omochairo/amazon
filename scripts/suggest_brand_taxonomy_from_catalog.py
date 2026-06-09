"""suggest_brand_taxonomy_from_catalog.py

P3 (epic #2126): 供給側 (カタログ) 起点の未登録ブランド検出器。
`suggest_brand_taxonomy_additions.py` (A-6 / 需要側 GSC) の姉妹。

需要側 (GSC by_query) は「検索流入のあるブランド」しか出せないため、
在庫はあるが未検索の有名メーカー (PicassoTiles / Tegu 等) が永遠に不可視だった
(session 136 で手動抽出した課題 #3)。本スクリプトは `data/articles/*.json` の
`product.brand` を全走査し、brand_taxonomy.yaml に未登録 (normalize → unknown)
のまま「ノーブランド」に埋没している raw 値を商品数降順で集計する。

アルゴリズム:
1. data/articles/*.json (サイドカー除外) を走査。各記事の product.brand を取得。
2. brand_normalizer.normalize(raw) が match_type == "unknown" かつ raw が
   非空のものだけを候補とする (空 raw = 真にブランド情報なし、別物)。
3. NON_BRAND_RAW (ノーブランド / Generic / 不明 等) は除外。
4. _fold(raw) で全半角・大小・記号差を畳んでグループ化 (表記ゆれ統合)。
   各グループは raw_variants / 代表 display / product_count / 例 (asin, title) を保持。
5. product_count >= min_products のグループを降順で top_n 件出力。

出力は「人間がレビューする候補リスト」(`data/analytics/catalog_brand_candidates.json`)。
alias 誤追加は全商品ページのブランド振り分けに波及するため auto-PR は作らない。
open_catalog_brand_issue.py が単一 Issue として提案を集約する。

Issue: https://github.com/omochairo/amazon/issues/2126 (epic / P3)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import pathlib
import sys
from typing import Any

# brand_normalizer は同じ scripts/ ディレクトリに在る
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brand_normalizer import _fold, get_taxonomy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("suggest_brand_taxonomy_from_catalog")

DEFAULT_ARTICLES_GLOB = "data/articles/*.json"
DEFAULT_OUT = "data/analytics/catalog_brand_candidates.json"
DEFAULT_MIN_PRODUCTS = 2
DEFAULT_TOP_N = 40
DEFAULT_EXAMPLES = 5

_SIDECAR_SUFFIXES = (".enrichment.json", ".quality.json", ".seo.json")

# product.brand に入りうる「ブランドではない」raw 値 (fold 済みで比較)。
# これらは候補から除外する (taxonomy に登録すべき実ブランドではない)。
NON_BRAND_RAW_FOLDED: set[str] = {
    _fold(s)
    for s in (
        "ノーブランド", "ノーブランド品", "ブランドなし", "なし",
        "Generic", "generic", "ジェネリック", "NoBrand", "No Brand",
        "不明", "メーカー不明", "-", "—", "その他", "Other",
        "Unknown", "N/A", "NA",
    )
}


def iter_article_files(articles_glob: str) -> list[str]:
    files = sorted(glob.glob(articles_glob))
    return [f for f in files if not f.endswith(_SIDECAR_SUFFIXES)]


def collect_candidates(files: list[str]) -> dict[str, Any]:
    tax = get_taxonomy()
    groups: dict[str, dict[str, Any]] = {}
    total_unknown = 0

    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("skip unreadable %s: %s", f, exc)
            continue
        product = d.get("product") or {}
        raw = (product.get("brand") or "").strip()
        if not raw:
            continue
        norm = tax.lookup(raw)
        if norm.match_type != "unknown":
            continue
        folded = _fold(raw)
        if not folded or folded in NON_BRAND_RAW_FOLDED:
            continue

        total_unknown += 1
        asin = product.get("asin") or d.get("asin") or ""
        title = product.get("title") or d.get("title") or ""
        grp = groups.setdefault(folded, {
            "fold_key": folded,
            "raw_counts": {},
            "product_count": 0,
            "examples": [],
        })
        grp["product_count"] += 1
        grp["raw_counts"][raw] = grp["raw_counts"].get(raw, 0) + 1
        if len(grp["examples"]) < DEFAULT_EXAMPLES:
            grp["examples"].append({
                "asin": asin,
                "title": title[:80],
                "raw": raw,
            })

    return {"groups": groups, "total_unknown_products": total_unknown}


def finalize(groups: dict[str, dict[str, Any]], *,
             min_products: int, top_n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for grp in groups.values():
        if grp["product_count"] < min_products:
            continue
        raw_counts: dict[str, int] = grp["raw_counts"]
        # display = 最頻 raw (同数なら長い方 = より情報量の多い表記)
        display = max(raw_counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        variants = sorted(raw_counts, key=lambda r: (-raw_counts[r], r))
        out.append({
            "fold_key": grp["fold_key"],
            "display": display,
            "raw_variants": variants,
            "product_count": grp["product_count"],
            "examples": grp["examples"],
        })
    out.sort(key=lambda g: (-g["product_count"], g["display"]))
    return out[:top_n]


def run(articles_glob: str, *, min_products: int, top_n: int) -> dict[str, Any]:
    files = iter_article_files(articles_glob)
    collected = collect_candidates(files)
    candidates = finalize(
        collected["groups"], min_products=min_products, top_n=top_n,
    )
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "params": {
            "min_products": min_products,
            "top_n": top_n,
            "articles_scanned": len(files),
        },
        "total_unknown_products": collected["total_unknown_products"],
        "total_unknown_groups": len(collected["groups"]),
        "candidates": candidates,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--articles-glob", default=DEFAULT_ARTICLES_GLOB)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-products", type=int, default=DEFAULT_MIN_PRODUCTS)
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    args = p.parse_args()

    result = run(
        args.articles_glob,
        min_products=args.min_products,
        top_n=args.top_n,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s — %d unknown products in %d groups, %d candidates (min_products=%d)",
        out, result["total_unknown_products"], result["total_unknown_groups"],
        len(result["candidates"]), args.min_products,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
