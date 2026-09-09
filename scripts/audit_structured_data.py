"""audit_structured_data.py

Issue #6821 項目1「全記事をスキャンし、Product / Review / AggregateRating /
AggregateOffer の欠損件数を数える」。

**測る対象を hugo/content/posts/ ではなく data/articles/ にしている理由**:
hugo/content/posts/*.md は .gitignore 対象の **ビルド生成物** であり、ローカルに
あるものは最後にローカルで build_post.py を回した時点のスナップショットにすぎない
(2026-09-09 実測: 同一ディレクトリ内で mtime が 2026-07-17 〜 2026-09-03 に散って
おり、49 記事が「当時は価格ゼロだった」せいで offers を持っていなかった。同じ
ASIN の data/articles/ は現在 valid price を持つ)。ローカルの md を数えると、
本番にも生成器にも存在しない欠損を報告してしまう。

そこで本スクリプトは **次の build が emit するもの** を数える:
data/articles/*.json を読み、build_post.py の `_fill_jsonld` /
`_extract_review_body` / `_build_review_jsonld` を **そのまま呼んで** JSON-LD を
再現する (deepcopy 上で実行するので data/articles には触らない。read-only)。
ロジックを写経しないので、生成側の分岐が変わればこの監査も自動的に追随する。

数えるもの (記事 1 件につき):
  - product          : Product JSON-LD が emit されるか
  - aggregateRating  : Product.aggregateRating を持つか
  - offers           : Product.offers が AggregateOffer として付くか
  - review           : 独立 Review JSON-LD が emit されるか (#1301 B5)
  - faq / howto / webpage : 併せて件数だけ数える (判断材料)
欠損は必ず **原因ラベル** とセットで数える (項目2「原因を分類して issue に
ぶら下げる」)。原因は生成側の分岐と 1:1 に対応させてある:
  - product_missing__no_product / __no_asin
  - offers_missing__price_unavailable   : 有効価格ゼロ (#4826 項目6 の分岐)
  - review_missing__no_review_body      : editorial_comment/narrative/verdict が空
  - review_missing__no_product_name     : product.name も title も空
  - faq_missing__no_faq_source
品質フラグ (欠損ではないが商品スニペットの適格性に効く):
  - product_image_empty / product_name_empty / product_brand_empty

breadcrumb は front matter からは emit されない (2026-09-09 実測で 0 件) が、
テーマ側の layouts/partials/templates/schema_json.html が BreadcrumbList を出す。
「front matter に breadcrumb が無い = 欠損」ではないので数えない。

出力:
  data/analytics/structured_data_audit.json
  samples だけ切り詰め、件数は必ず切り詰め前を残す。

使い方:
  python -m scripts.audit_structured_data
  python -m scripts.audit_structured_data --limit 50 --out /tmp/x.json
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from scripts.build_post import (
    _build_howto_jsonld,
    _build_review_jsonld,
    _extract_review_body,
    _fill_jsonld,
)
from scripts.compute_semantic_related import discover_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_structured_data")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "data/analytics/structured_data_audit.json"
DEFAULT_MAX_SAMPLES = 20

ENTITIES = ("product", "aggregateRating", "offers", "review", "faq", "howto", "webpage")


# --------------------------------------------------------------------------
# 1 記事の判定 (pure)
# --------------------------------------------------------------------------

def audit_article(data: dict[str, Any]) -> dict[str, Any]:
    """記事 JSON 1 件について、次の build が emit する JSON-LD を再現して数える。

    引数は変更しない (deepcopy 上で `_fill_jsonld` を呼ぶ)。
    """
    work = copy.deepcopy(data)
    product = work.get("product") or {}
    asin = product.get("asin") if isinstance(product, dict) else ""

    present: dict[str, bool] = {k: False for k in ENTITIES}
    causes: list[str] = []
    flags: list[str] = []

    if not isinstance(product, dict) or not product:
        causes.append("product_missing__no_product")
        return {"present": present, "causes": causes, "flags": flags}

    _fill_jsonld(work)
    jsonld = work.get("jsonld") or {}
    product_ld = jsonld.get("product") if isinstance(jsonld.get("product"), dict) else {}

    present["product"] = bool(product_ld)
    if not product_ld:
        causes.append("product_missing__no_product")
        return {"present": present, "causes": causes, "flags": flags}

    present["aggregateRating"] = isinstance(product_ld.get("aggregateRating"), dict)
    offers = product_ld.get("offers")
    present["offers"] = isinstance(offers, dict) and offers.get("@type") == "AggregateOffer"
    if not present["offers"]:
        # `_fill_jsonld` が offers を落とす唯一の条件 = 有効価格ゼロ
        # (valid prices も best_price も無い)。#4826 項目6。
        causes.append("offers_missing__price_unavailable")

    faq_ld = jsonld.get("faq")
    present["faq"] = isinstance(faq_ld, dict) and bool(faq_ld.get("mainEntity"))
    if not present["faq"]:
        causes.append("faq_missing__no_faq_source")

    # webpage / review / howto は build_post 側で `if product and asin` に入る。
    if not asin:
        causes.append("product_missing__no_asin")
        return {"present": present, "causes": causes, "flags": flags}
    present["webpage"] = True

    review_body = _extract_review_body(work)
    if not review_body:
        causes.append("review_missing__no_review_body")
    else:
        summary = work.get("review_summary")
        avg_rating: Any = 4.0
        if isinstance(summary, dict) and summary.get("avg_rating") is not None:
            avg_rating = summary["avg_rating"]
        elif product.get("ivs_score") is not None:
            avg_rating = product["ivs_score"]
        review_ld = _build_review_jsonld(
            product=product,
            title=str(work.get("title") or ""),
            date=str(work.get("date") or ""),
            avg_rating=avg_rating,
            review_body=review_body,
            asin=str(asin),
            aggregate_rating=product_ld.get("aggregateRating"),
            offers=product_ld.get("offers"),
        )
        present["review"] = bool(review_ld)
        if not review_ld:
            causes.append("review_missing__no_product_name")

    # HowTo は「選び方」ステップが組めた記事だけに付く任意エンティティ。
    # 欠損は不具合ではないので原因ラベルを付けず、件数だけ数える。
    present["howto"] = bool(_build_howto_jsonld(product=product, data=work, asin=str(asin)))

    if not product.get("image"):
        flags.append("product_image_empty")
    if not (product.get("name") or work.get("title")):
        flags.append("product_name_empty")
    if not product.get("brand"):
        flags.append("product_brand_empty")

    return {"present": present, "causes": causes, "flags": flags}


# --------------------------------------------------------------------------
# 全数スキャン
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    *,
    limit: int | None = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> dict[str, Any]:
    paths = discover_articles(articles_dir)
    items = sorted(paths.items())
    if limit:
        items = items[:limit]

    present_counts = {k: 0 for k in ENTITIES}
    cause_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    unreadable: list[str] = []
    total = 0

    for asin, path in items:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip %s: %s", path.name, exc)
            unreadable.append(path.name)
            continue
        if not isinstance(data, dict):
            unreadable.append(path.name)
            continue
        total += 1
        res = audit_article(data)
        for key, ok in res["present"].items():
            if ok:
                present_counts[key] += 1
        for label in list(res["causes"]) + list(res["flags"]):
            bucket = cause_counts if label in res["causes"] else flag_counts
            bucket[label] = bucket.get(label, 0) + 1
            hits = samples.setdefault(label, [])
            if len(hits) < max_samples:
                hits.append(asin)

    missing_counts = {k: total - v for k, v in present_counts.items()}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles_dir": str(articles_dir),
        "articles_total": total,
        "unreadable": unreadable,
        "present": present_counts,
        "missing": missing_counts,
        "causes": dict(sorted(cause_counts.items())),
        "flags": dict(sorted(flag_counts.items())),
        "samples": {k: v for k, v in sorted(samples.items())},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ見る (0 = 全件)")
    ap.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    args = ap.parse_args(argv)

    report = run(
        pathlib.Path(args.articles_dir),
        limit=args.limit or None,
        max_samples=args.max_samples,
    )
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("articles=%d -> %s", report["articles_total"], out)
    for key in ENTITIES:
        logger.info(
            "  %-16s present=%4d missing=%4d",
            key,
            report["present"][key],
            report["missing"][key],
        )
    for label, n in report["causes"].items():
        logger.info("  cause %-38s %4d", label, n)
    for label, n in report["flags"].items():
        logger.info("  flag  %-38s %4d", label, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
