"""Build aggregated feature lists (/cospa/ and /deals/) from existing article data.

Refs: GitHub issue #590

Reads:
    data/articles/*.json                      (article body; excludes *.quality.json etc.)
    data/raw/per_asin/<ASIN>/amazon.json      (savings_percentage / fetched_at)

Writes:
    hugo/data/features/cospa.json             (monthly: IVS x best_price efficiency TOP N)
    hugo/data/features/deals.json             (weekly: savings_percentage TOP N, stale-guarded)
    data/features/_build_manifest.json        (pool sizes / drop reasons, ref #677)

Pure aggregation only - no external API calls (ref feedback: build_post no in-band I/O).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# 知育スコアは build_post.py と同じ score_calculator で再計算する。
# Jules が保存した data/articles/*.json の生 ivs_score / ivs_detail.total_100 は
# 旧スコア (Jules 推定) であり、build_post の _sync_ivs_for_render で記事ページ表示時に
# 再計算されている。/cospa/ /deals/ がここを読まず Jules 生値を出していたため
# 「list 100 / 詳細 81」の乖離が出ていた (session 58)。同じ algorithm で再計算する。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand_normalizer import normalize as normalize_brand  # noqa: E402
from score_calculator import calculate as calculate_score  # noqa: E402

logger = logging.getLogger("build_feature_lists")

ARTICLE_SKIP_SUFFIXES = (".quality.json", ".enrichment.json", ".seo.json")


@dataclass
class ArticleRecord:
    """Minimal projection of an article needed for feature list aggregation."""

    asin: str
    slug: str
    name: str | None
    image: str | None
    ivs_score: float | None
    ivs_100: int | None
    best_price: int | None
    best_platform: str | None
    amazon_url: str | None
    savings_percentage: int | None = None
    fetched_at: str | None = None
    score_cospa: float | None = field(default=None, init=False)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _is_article_json(path: Path) -> bool:
    name = path.name
    if not name.endswith(".json"):
        return False
    return not any(name.endswith(suf) for suf in ARTICLE_SKIP_SUFFIXES)


def load_articles(articles_dir: Path) -> list[ArticleRecord]:
    """Read every article json under ``articles_dir`` into an ArticleRecord list.

    Articles missing required fields (asin, ivs_score, best_price) are skipped
    with a debug log; the caller can rely on every returned record being usable.
    """
    records: list[ArticleRecord] = []
    if not articles_dir.exists():
        logger.warning("articles_dir does not exist: %s", articles_dir)
        return records

    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip unreadable article %s: %s", path.name, exc)
            continue

        prod = raw.get("product") or {}
        asin = prod.get("asin")
        best_price = prod.get("best_price")
        if not asin or prod.get("ivs_score") is None or best_price is None:
            logger.debug("skip article missing required fields: %s", path.name)
            continue

        prices = prod.get("prices") or {}
        amazon_block = prices.get("amazon") or {}

        # 記事ページと同じ式で再計算 (score_calculator)。生 JSON の ivs_score は
        # Jules 推定で stale。brand が無い場合は normalize_brand("") が unknown を返す。
        brand = normalize_brand(prod.get("brand") or "")
        sr = calculate_score(raw, brand, asin=asin)

        records.append(
            ArticleRecord(
                asin=str(asin),
                slug=str(raw.get("slug") or path.stem),
                name=prod.get("name"),
                image=prod.get("image"),
                ivs_score=sr.ivs_score,
                ivs_100=sr.total_100,
                best_price=_safe_int(best_price),
                best_platform=prod.get("best_platform"),
                amazon_url=amazon_block.get("url"),
            )
        )

    return records


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def attach_amazon_meta(records: list[ArticleRecord], per_asin_dir: Path) -> None:
    """Mutate ``records`` in place, filling ``savings_percentage`` / ``fetched_at``.

    Missing per_asin/<ASIN>/amazon.json files are silently skipped - the deals
    builder simply won't include those records.
    """
    for rec in records:
        meta_path = per_asin_dir / rec.asin / "amazon.json"
        if not meta_path.exists():
            continue
        try:
            with meta_path.open(encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skip per_asin meta %s: %s", meta_path, exc)
            continue
        item = meta.get("item") or {}
        rec.savings_percentage = _safe_int(item.get("savings_percentage"))
        rec.fetched_at = meta.get("fetched_at")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _dedupe_by_asin(records: Iterable[ArticleRecord]) -> list[ArticleRecord]:
    """Keep the highest-IVS record per ASIN.

    Multiple articles can target the same ASIN (re-runs, regenerations). For
    feature lists we want one card per product; we prefer the higher IVS so
    re-scored regenerations win over legacy entries.
    """
    by_asin: dict[str, ArticleRecord] = {}
    for rec in records:
        existing = by_asin.get(rec.asin)
        if existing is None:
            by_asin[rec.asin] = rec
            continue
        cur = rec.ivs_score or 0.0
        prev = existing.ivs_score or 0.0
        if cur > prev:
            by_asin[rec.asin] = rec
    return list(by_asin.values())


def _cospa_score(ivs_100: int, best_price: int) -> float:
    """Return cost-performance efficiency score.

    Defined as (ivs_100 / 100) / log10(best_price + 100). The +100 floor keeps
    very cheap items (price -> 0) from blowing up the score; the log keeps
    high-IVS items at ¥3000 from being dominated by anything cheaper.
    """
    return (ivs_100 / 100.0) / math.log10(best_price + 100)


# /cospa/ 価格帯ナビ (Issue: session 58 ユーザー要望)。
# 「全 TOP20 で IVS 寄り集計だと知育スコアランキングと変わらない & トミカ/レゴで埋まる」
# を解消するため、価格帯ごとに TOP-N を出す。デフォルト表示は 3,000-5,000 円
# (一番選びにくく、贈答需要が高い帯)。
PRICE_BANDS: list[dict[str, Any]] = [
    {"key": "lt1000",     "label": "〜¥1,000",       "price_min": 0,     "price_max": 999},
    {"key": "1000-2000",  "label": "¥1,000-¥2,000",  "price_min": 1000,  "price_max": 1999},
    {"key": "2000-3000",  "label": "¥2,000-¥3,000",  "price_min": 2000,  "price_max": 2999},
    {"key": "3000-5000",  "label": "¥3,000-¥5,000",  "price_min": 3000,  "price_max": 4999, "default": True},
    {"key": "5000-7000",  "label": "¥5,000-¥7,000",  "price_min": 5000,  "price_max": 6999},
    {"key": "7000-10000", "label": "¥7,000-¥10,000", "price_min": 7000,  "price_max": 9999},
]


def build_cospa(
    records: Iterable[ArticleRecord],
    *,
    min_ivs: float = 4.0,
    price_min: int = 500,
    price_max: int = 5000,
    top_n: int = 20,
    sort_key: str = "cospa",
) -> tuple[list[ArticleRecord], dict[str, int]]:
    """Select TOP-N cospa picks.

    ``sort_key`` controls ordering:
      - ``"cospa"`` (default): cospa efficiency (ivs_100 / log(price)) 降順。
        帯横断 TOP-N で「IVS と価格のバランス」を見たいときに使う。
      - ``"ivs"``: 知育スコア (ivs_100) 降順 → 価格 昇順。
        価格帯ナビのように既に帯を固定しているケースで、UI 文言
        「知育スコアの高い順」と一致させるために使う (帯内 cospa 順だと
        IVS が高いのに安くないせいで下位になり、文言と矛盾する)。
    """
    drops = {"low_ivs": 0, "price_out_of_band": 0, "missing_ivs_100": 0}
    survivors: list[ArticleRecord] = []
    for rec in _dedupe_by_asin(records):
        if rec.ivs_score is None or rec.ivs_score < min_ivs:
            drops["low_ivs"] += 1
            continue
        if rec.best_price is None or not (price_min <= rec.best_price <= price_max):
            drops["price_out_of_band"] += 1
            continue
        if rec.ivs_100 is None:
            drops["missing_ivs_100"] += 1
            continue
        rec.score_cospa = _cospa_score(rec.ivs_100, rec.best_price)
        survivors.append(rec)

    if sort_key == "ivs":
        survivors.sort(
            key=lambda r: (-(r.ivs_100 or 0), r.best_price or 0),
        )
    else:
        survivors.sort(
            key=lambda r: (-(r.score_cospa or 0.0), -(r.ivs_100 or 0)),
        )
    return survivors[:top_n], drops


def build_cospa_bands(
    records: Iterable[ArticleRecord],
    *,
    min_ivs: float = 4.0,
    top_n_per_band: int = 10,
) -> list[dict[str, Any]]:
    """Build per-price-band cospa lists.

    各帯の中では「知育スコア (ivs_100) 降順 → 価格 昇順」で並べる。
    cospa score (IVS/log(price)) は帯横断比較のための式なので、帯内では
    純粋にスコアの高い順を見せたほうがユーザにとってわかりやすい。
    (UI 文言 feature.html:74 「知育スコアの高い順に並べています」と整合)
    """
    # records は generator の可能性があるため一度 list 化 (各帯で再走査するため)
    records_list = list(_dedupe_by_asin(records))
    bands_out: list[dict[str, Any]] = []
    for band in PRICE_BANDS:
        items, _drops = build_cospa(
            records_list,
            min_ivs=min_ivs,
            price_min=band["price_min"],
            price_max=band["price_max"],
            top_n=top_n_per_band,
            sort_key="ivs",
        )
        bands_out.append({
            **band,
            "count": len(items),
            "items": items,
        })
    return bands_out


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Python's fromisoformat accepts "+00:00" but not the "Z" suffix until 3.11.
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_deals(
    records: Iterable[ArticleRecord],
    *,
    min_ivs: float = 4.0,
    min_savings: int = 20,
    stale_days: int = 14,
    top_n: int = 20,
    now: datetime | None = None,
) -> tuple[list[ArticleRecord], dict[str, int]]:
    """Select TOP-N discount picks.

    Stale guard: per_asin/amazon.json's ``fetched_at`` must be within
    ``stale_days`` of ``now``. Without this, a deal that ended weeks ago
    could keep appearing on /deals/.
    """
    drops = {
        "low_ivs": 0,
        "no_savings_data": 0,
        "savings_below_threshold": 0,
        "stale_or_unknown_fetch": 0,
    }
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_days)

    survivors: list[ArticleRecord] = []
    for rec in _dedupe_by_asin(records):
        if rec.ivs_score is None or rec.ivs_score < min_ivs:
            drops["low_ivs"] += 1
            continue
        if rec.savings_percentage is None:
            drops["no_savings_data"] += 1
            continue
        if rec.savings_percentage < min_savings:
            drops["savings_below_threshold"] += 1
            continue
        fetched = _parse_iso8601(rec.fetched_at)
        if fetched is None or fetched < cutoff:
            drops["stale_or_unknown_fetch"] += 1
            continue
        survivors.append(rec)

    survivors.sort(
        key=lambda r: (-(r.savings_percentage or 0), -(r.ivs_100 or 0)),
    )
    return survivors[:top_n], drops


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _record_to_payload_common(rec: ArticleRecord, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "asin": rec.asin,
        "slug": rec.slug,
        "url_internal": f"/posts/{rec.slug.lower()}/",  # Hugo lowercases URLs
        "name": rec.name,
        "image": rec.image,
        "ivs_100": rec.ivs_100,
        "ivs_score": rec.ivs_score,
        "best_price": rec.best_price,
        "best_platform": rec.best_platform,
        "amazon_url": rec.amazon_url,
    }


def serialize_cospa_bands(
    bands: list[dict[str, Any]],
    *,
    filter_params: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """価格帯ナビ用シリアライズ。各 band の items を payload に変換する。"""
    payload_bands = []
    total = 0
    for band in bands:
        payload_items = []
        for idx, rec in enumerate(band["items"], start=1):
            entry = _record_to_payload_common(rec, idx)
            entry["score_cospa"] = (
                round(rec.score_cospa, 4) if rec.score_cospa is not None else None
            )
            payload_items.append(entry)
        total += len(payload_items)
        payload_bands.append({
            "key": band["key"],
            "label": band["label"],
            "price_min": band["price_min"],
            "price_max": band["price_max"],
            "default": bool(band.get("default")),
            "count": len(payload_items),
            "items": payload_items,
        })
    return {
        "generated_at": generated_at,
        "type": "cospa",
        "filter": filter_params,
        "count": total,
        "bands": payload_bands,
    }


def serialize_deals(
    items: list[ArticleRecord],
    *,
    filter_params: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    payload_items = []
    for idx, rec in enumerate(items, start=1):
        entry = _record_to_payload_common(rec, idx)
        entry["savings_percentage"] = rec.savings_percentage
        entry["fetched_at"] = rec.fetched_at
        payload_items.append(entry)
    return {
        "generated_at": generated_at,
        "type": "deals",
        "filter": filter_params,
        "count": len(payload_items),
        "items": payload_items,
    }


def write_outputs(
    cospa_payload: dict[str, Any],
    deals_payload: dict[str, Any],
    manifest: dict[str, Any],
    *,
    out_hugo: Path,
    out_manifest: Path,
) -> None:
    out_hugo.mkdir(parents=True, exist_ok=True)
    out_manifest.parent.mkdir(parents=True, exist_ok=True)

    (out_hugo / "cospa.json").write_text(
        json.dumps(cospa_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_hugo / "deals.json").write_text(
        json.dumps(deals_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    *,
    articles_dir: Path,
    per_asin_dir: Path,
    out_hugo: Path,
    out_manifest: Path,
    top_n: int = 20,
    min_ivs: float = 4.0,
    price_min: int = 500,
    price_max: int = 5000,
    min_savings: int = 20,
    stale_days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full pipeline. Returns the manifest dict for testability."""
    records = load_articles(articles_dir)
    attach_amazon_meta(records, per_asin_dir)

    cospa_bands = build_cospa_bands(
        records,
        min_ivs=min_ivs,
        top_n_per_band=top_n,
    )
    deals_items, deals_drops = build_deals(
        records,
        min_ivs=min_ivs,
        min_savings=min_savings,
        stale_days=stale_days,
        top_n=top_n,
        now=now,
    )

    generated_at = _now_iso()
    cospa_filter = {
        "min_ivs": min_ivs,
        "top_n_per_band": top_n,
        "bands": [
            {"key": b["key"], "price_min": b["price_min"], "price_max": b["price_max"]}
            for b in cospa_bands
        ],
    }
    deals_filter = {
        "min_ivs": min_ivs,
        "min_savings": min_savings,
        "stale_days": stale_days,
        "top_n": top_n,
    }

    cospa_payload = serialize_cospa_bands(
        cospa_bands, filter_params=cospa_filter, generated_at=generated_at
    )
    deals_payload = serialize_deals(
        deals_items, filter_params=deals_filter, generated_at=generated_at
    )

    manifest = {
        "generated_at": generated_at,
        "articles_loaded": len(records),
        "cospa": {
            "bands": {b["key"]: b["count"] for b in cospa_bands},
            "filter": cospa_filter,
        },
        "deals": {
            "count": len(deals_items),
            "filter": deals_filter,
            "drops": deals_drops,
        },
    }

    write_outputs(
        cospa_payload,
        deals_payload,
        manifest,
        out_hugo=out_hugo,
        out_manifest=out_manifest,
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", default="data/articles", type=Path)
    parser.add_argument("--per-asin-dir", default="data/raw/per_asin", type=Path)
    parser.add_argument("--out-hugo", default="hugo/data/features", type=Path)
    parser.add_argument(
        "--out-manifest",
        default="data/features/_build_manifest.json",
        type=Path,
    )
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-ivs", type=float, default=4.0)
    parser.add_argument("--price-min", type=int, default=500)
    parser.add_argument("--price-max", type=int, default=5000)
    parser.add_argument("--min-savings", type=int, default=20)
    parser.add_argument("--stale-days", type=int, default=14)
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    manifest = run(
        articles_dir=args.articles_dir,
        per_asin_dir=args.per_asin_dir,
        out_hugo=args.out_hugo,
        out_manifest=args.out_manifest,
        top_n=args.top_n,
        min_ivs=args.min_ivs,
        price_min=args.price_min,
        price_max=args.price_max,
        min_savings=args.min_savings,
        stale_days=args.stale_days,
    )
    cospa_total = sum(manifest["cospa"].get("bands", {}).values())
    logger.info(
        "built: cospa=%d (across %d bands) deals=%d (from %d articles)",
        cospa_total,
        len(manifest["cospa"].get("bands", {})),
        manifest["deals"]["count"],
        manifest["articles_loaded"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


