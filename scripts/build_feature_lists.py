"""Build aggregated feature lists (/cospa/ and /deals/) from existing article data.

Refs: GitHub issue #590

Reads:
    data/articles/*.json                      (article body; excludes *.quality.json etc.)
    data/raw/per_asin/<ASIN>/amazon.json      (savings_percentage / fetched_at)
    data/raw/{rakuten,yahoo}_matched.json     (#4007 follow-up 1: 楽天/Yahoo 現在価格)

Writes:
    hugo/data/features/cospa.json             (IVS x best_price efficiency TOP N / 価格帯別)
    hugo/data/features/deals.json             (savings_percentage TOP N, stale-guarded)
    data/features/_build_manifest.json        (pool sizes / drop reasons, ref #677)

更新頻度 (2026-07-26 / #4007 で変更):
    以前は GitHub 09-feature-lists.yml の cron (cospa=月次 / deals=週次) が
    hugo/data/features/*.json を commit する唯一の更新経路で、入力データ
    (per_asin 中央値2日 / price_watch 0.6日) よりページの方が古かった。現在は
    .gitlab-ci.yml の pages job (配信ビルド) が毎デプロイこのスクリプトを走らせる
    ビルド時派生であり、commit 済み JSON は step 失敗時の fallback として残す。
    価格は price_overlay 経由で日次観測 (price_watch) を優先して上書きする。

Pure aggregation only - no external API calls (ref feedback: build_post no in-band I/O).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
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
import market_prices  # noqa: E402
import price_overlay  # noqa: E402
from brand_normalizer import normalize as normalize_brand  # noqa: E402
from score_calculator import calculate as calculate_score, compute_ivs_axes  # noqa: E402

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
    # #4007: プラットフォーム別価格。best_price を Amazon の最新観測で再計算する
    # ために保持する (記事 JSON の best_price は記事生成時のまま凍結している)。
    price_amazon: int | None = None
    price_rakuten: int | None = None
    price_yahoo: int | None = None
    # 4 軸 0-5 (#589 — feature-item.html がカード上で簡易バーを描く)。
    ivs_axes: dict[str, float] | None = None
    # 対象年齢の最小月数 (#3563 カード情報統一 / cospa・deals・テーマ hub 全ての
    # feature JSON に伝播する)。ソース優先順は build_post.py L2668-2699 と同じ。
    age_min_months: int | None = None
    score_cospa: float | None = field(default=None, init=False)


# ---------------------------------------------------------------------------
# Age parsing (#3563: build_category_hubs.py から移動。build_category_hubs は
# こちらを import する形に変更済み。呼出元の挙動は不変)
# ---------------------------------------------------------------------------

def parse_min_months(age_range: str | None) -> int | None:
    """persona_fit.age_range 文字列から最小推奨年齢を月数で抽出。

    例: "3歳以上"/"3歳〜"/"3歳" → 36, "1歳6ヶ月〜"/"1.5歳"/"1歳半" → 18,
    "6ヶ月〜" → 6, "対象年齢の記載なし" → None。複数表記 (歳半=.5歳=6ヶ月) を吸収。
    """
    if not age_range:
        return None
    s = age_range.strip()
    if "記載" in s or "なし" in s:
        return None
    s = s.replace("歳半", ".5歳").replace("才", "歳")
    # 「N歳Mヶ月」形式
    m = re.search(r"(\d+)\s*歳\s*(\d+)\s*[ヶか]?月", s)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    # 「N歳」「N.5歳」形式
    m = re.search(r"(\d+(?:\.\d+)?)\s*歳", s)
    if m:
        return int(round(float(m.group(1)) * 12))
    # 「Nヶ月」形式
    m = re.search(r"(\d+)\s*[ヶか]?月", s)
    if m:
        return int(m.group(1))
    return None


def age_min_months_from_article(raw: dict[str, Any]) -> int | None:
    """記事 json (raw dict全体) から対象年齢の最小月数を抽出する。

    フィールド優先順は build_post.py L2668-2699 (raw_age 抽出部) と同じ:
    product.target_age or product.age_range -> persona_fit.age_range ->
    technical_specs.age_range。build_post.py 自体は変更しない (仕様固定)。
    """
    product = raw.get("product") or {}
    age_range = (
        product.get("target_age")
        or product.get("age_range")
        or (raw.get("persona_fit") or {}).get("age_range")
        or (raw.get("technical_specs") or {}).get("age_range")
    )
    return parse_min_months(age_range)


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

    Articles missing required fields (asin, best_price) are skipped with a
    debug log; the caller can rely on every returned record being usable.
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
        # #4007 follow-up 4: ここでは asin と best_price のみを必須にする。
        # 従来は `prod.get("ivs_score") is None` も gate に入れていたが、その
        # raw ivs_score はすぐ下の calculate_score() で再計算して捨てるだけの
        # 値であり (Jules 推定で stale)、「使わない値の presence check」で記事を
        # 除外していた。実測 (2026-07-27): 1713 記事中 1113 件がこの check だけで
        # 特集ページ (/cospa/ /deals/ + hub) の候補プールから外れており、うち
        # 全件が asin/best_price を保有していた。除外 1113 件を calculate_score
        # に通すと全件成功し (例外 0)、ivs_score の分布 (median 4.00) も現行
        # プール (4.15) とほぼ同じで、--min-ivs 4.0 を 569 件が通過した。
        if not asin or best_price is None:
            logger.debug("skip article missing required fields: %s", path.name)
            continue

        prices = prod.get("prices") or {}
        amazon_block = prices.get("amazon") or {}

        # 記事ページと同じ式で再計算 (score_calculator)。生 JSON の ivs_score は
        # Jules 推定で stale。brand が無い場合は normalize_brand("") が unknown を返す。
        # ivs_score gate 撤去でプール対象が 1713 記事全体に広がるため、
        # calculate_score が想定外の例外を投げた場合に備えて防御する
        # (実測では 0 件だが、母数が増えるので fail-soft にしておく)。
        brand = normalize_brand(prod.get("brand") or "")
        try:
            sr = calculate_score(raw, brand, asin=asin)
        except Exception as exc:  # noqa: BLE001 - fail-soft, log and skip
            logger.debug("skip article: calculate_score raised for %s: %s", path.name, exc)
            continue

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
                ivs_axes=compute_ivs_axes(sr.breakdown),
                age_min_months=age_min_months_from_article(raw),
                price_amazon=_safe_int(amazon_block.get("price")),
                price_rakuten=_safe_int((prices.get("rakuten") or {}).get("price")),
                price_yahoo=_safe_int((prices.get("yahoo") or {}).get("price")),
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


_BEST_PLATFORM_LABELS = (
    ("price_amazon", "Amazon"),
    ("price_rakuten", "楽天市場"),
    ("price_yahoo", "Yahoo!ショッピング"),
)


def _recompute_best_price(rec: ArticleRecord) -> None:
    """全プラットフォーム最安を ``best_price`` / ``best_platform`` に反映する。

    build_post._recompute_best_price と同じラベル・同じ規則 (0/None は候補外)。
    """
    candidates = [
        (getattr(rec, attr), label)
        for attr, label in _BEST_PLATFORM_LABELS
        if isinstance(getattr(rec, attr), int) and getattr(rec, attr) > 0
    ]
    if not candidates:
        return
    best = min(candidates, key=lambda x: x[0])
    rec.best_price = best[0]
    rec.best_platform = best[1]


def _apply_market_price(
    rec: ArticleRecord,
    platform: str,
    index: dict[str, dict[str, Any]] | None,
    amazon_price: int,
    stats: dict[str, int],
) -> bool:
    """1 プラットフォーム分の楽天/Yahoo 価格を matched JSON で解決し ``rec`` を更新する。

    build_post._attach_market_prices と同じ優先順位 (market_prices.resolve_price)
    を使う。``index`` が None (raw_root 未指定) のときは何もしない。

    戻り値は価格が変わったか (呼び出し側の best_price 再計算要否判定に使う)。
    """
    if index is None:
        return False
    field = f"price_{platform}"
    existing_price = getattr(rec, field) or 0
    matched = index.get(rec.asin)
    new_price = market_prices.resolve_price(existing_price, matched, amazon_price)

    if matched and market_prices.matched_passes_quality(matched, amazon_price):
        stats[f"market_{platform}"] += 1
    elif existing_price > 0 and new_price == 0:
        # matched が無い/gate 落ちで、かつ既存値が Amazon 価格の 3.0x 超 / 1/3 未満の
        # extreme outlier として破棄された (market_prices.PRICE_BAND_EXTREME)。
        stats["market_extreme_dropped"] += 1

    if new_price != existing_price:
        setattr(rec, field, new_price)
        return True
    return False


def overlay_current_prices(
    records: list[ArticleRecord],
    per_asin_dir: Path,
    *,
    watch_index: dict[str, price_overlay.PriceObservation] | None = None,
    now: datetime | None = None,
    raw_root: Path | None = None,
) -> dict[str, int]:
    """#4007: Amazon/楽天/Yahoo 価格を日次観測・matched JSON で上書きし
    ``best_price`` を再計算する。

    記事 JSON の ``product.best_price`` は記事生成時のまま凍結しており、実測で
    価格帯バケットが変わるものが 98 件 (6.5%) あった。/deals/ /cospa/ と年齢/
    テーマ hub のカードは記事ページと同じ観測を出す必要があるため、
    build_post.py と同じ price_overlay (price_watch 日次 → per_asin 週次) を
    通す。観測が無い ASIN は記事 JSON の値のまま (fail-soft)。

    ``raw_root`` を渡したときのみ、build_post.py と同じ ``data/raw/
    {rakuten,yahoo}_matched.json`` (#4007 follow-up 1: market_prices モジュール
    共有) で楽天/Yahoo 価格を検証/上書きする。上書き後の Amazon 価格
    (``rec.price_amazon``) を anchor に quality gate を通し、gate 落ち・
    extreme outlier は既存値を維持または破棄する (build_post と同じ規則)。
    ``raw_root=None`` (既定) では従来どおり楽天/Yahoo は記事 JSON の値のまま
    変更しない (既存呼び出し元・テストの挙動を壊さないため)。

    戻り値は source 別の適用件数 (price_watch / per_asin / 観測なし ``none``、
    matched で更新した ``market_rakuten`` / ``market_yahoo``、extreme outlier
    として既存値を破棄した ``market_extreme_dropped``)。
    """
    if watch_index is None:
        watch_index = price_overlay.load_watch_index(now=now)
    stats = {
        "price_watch": 0,
        "per_asin": 0,
        "none": 0,
        "market_rakuten": 0,
        "market_yahoo": 0,
        "market_extreme_dropped": 0,
    }

    rakuten_index: dict[str, dict[str, Any]] | None = None
    yahoo_index: dict[str, dict[str, Any]] | None = None
    if raw_root is not None:
        rakuten_index = market_prices.load_matched_index(raw_root / "rakuten_matched.json")
        yahoo_index = market_prices.load_matched_index(raw_root / "yahoo_matched.json")

    for rec in records:
        obs = price_overlay.resolve(
            rec.asin, watch_index=watch_index, per_asin_root=per_asin_dir, now=now
        )
        needs_recompute = False
        if obs is None:
            stats["none"] += 1
        else:
            stats[obs.source] += 1
            if obs.price is not None:
                rec.price_amazon = obs.price
                needs_recompute = True
            if obs.savings_percentage is not None:
                rec.savings_percentage = obs.savings_percentage
            if obs.observed_at:
                rec.fetched_at = obs.observed_at

        if rakuten_index is not None or yahoo_index is not None:
            amazon_price = rec.price_amazon or 0
            if _apply_market_price(rec, "rakuten", rakuten_index, amazon_price, stats):
                needs_recompute = True
            if _apply_market_price(rec, "yahoo", yahoo_index, amazon_price, stats):
                needs_recompute = True

        if needs_recompute:
            _recompute_best_price(rec)

    return stats


def attach_amazon_meta(
    records: list[ArticleRecord],
    per_asin_dir: Path,
    *,
    watch_index: dict[str, price_overlay.PriceObservation] | None = None,
    now: datetime | None = None,
    raw_root: Path | None = None,
) -> None:
    """Mutate ``records`` in place, filling ``savings_percentage`` / ``fetched_at``.

    Missing per_asin/<ASIN>/amazon.json files are silently skipped - the deals
    builder simply won't include those records.

    #4007: per_asin (週次) を読んだうえで、より新しい観測があれば
    ``overlay_current_prices`` で価格・割引率・観測時刻を上書きする。
    ``raw_root`` を渡すと楽天/Yahoo も matched JSON で更新する (follow-up 1)。
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

    stats = overlay_current_prices(
        records, per_asin_dir, watch_index=watch_index, now=now, raw_root=raw_root
    )
    logger.info(
        "price_overlay: %d price_watch / %d per_asin / %d no observation",
        stats["price_watch"], stats["per_asin"], stats["none"],
    )
    logger.info(
        "market_prices: %d rakuten / %d yahoo updated from matched JSON, "
        "%d extreme outlier dropped",
        stats["market_rakuten"], stats["market_yahoo"], stats["market_extreme_dropped"],
    )


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
    stale_days: int = 3,
    top_n: int = 20,
    now: datetime | None = None,
) -> tuple[list[ArticleRecord], dict[str, int]]:
    """Select TOP-N discount picks.

    Stale guard: per_asin/amazon.json's ``fetched_at`` (overwritten by
    ``overlay_current_prices`` with price_watch's ``observed_at`` when a
    fresher observation exists) must be within ``stale_days`` of ``now``.
    Without this, a deal that ended weeks ago could keep appearing on
    /deals/.

    ``stale_days`` default is 3, not 14 (#4007 follow-up 2). 14 日は
    price_overlay 導入前 (per_asin 週次巡回のみ) の名残で、日次観測
    (price_watch) が入力になった現在は分布の遥か外側にあり選別として
    機能していなかった: 実測 (2026-07-27) で deals 対象 (savings>=20%) 155
    件中、fetched_at が観測 1 日以内のもの 154 件・3 日以内も同じく 154
    件・14 日以内も 155 件 — 14 日ガードは 1 件も除外していなかった。
    3 日に締めても減るのは 1 件のみ。呼び出し側は通常この関数を直接では
    なく ``build_deals_with_stale_fallback`` 経由で使い、NAS 日次観測レーン
    (price_watch) が数日止まって pool が薄くなった場合に窓を自動で広げる。
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


# 段階的 stale window フォールバック (#4007 follow-up 2)。base window (既定 3日) を
# 締めた副作用として「NAS レーン (price_watch) が数日止まると /deals/ が空になる」
# 失敗モードが出るのを避けるため、採用カード数が下限を割ったら 7日 -> 14日 と
# 順に広げて再試行する。ユーザが --stale-days で base を 7 や 20 に変えた場合は
# 7/14 のうち base 以上のものだけを候補にする (base より狭い窓には戻さない)。
_STALE_FALLBACK_LADDER_ANCHORS = (7, 14)


def build_deals_with_stale_fallback(
    records: Iterable[ArticleRecord],
    *,
    min_ivs: float = 4.0,
    min_savings: int = 20,
    stale_days: int = 3,
    stale_min_cards: int = 8,
    top_n: int = 20,
    now: datetime | None = None,
) -> tuple[list[ArticleRecord], dict[str, int], int]:
    """``build_deals`` を stale window を段階的に広げながら呼び出す。

    まず ``stale_days`` (既定 3) で pool を作る。採用カード数
    (top_n 適用後の枚数) が ``stale_min_cards`` (既定 8、``top_n`` より
    小さい値を想定) を割ったら、7日 -> 14日 の順に窓を広げて再試行する
    (``stale_days`` が既に 7 や 14 以上ならその値は候補から外す)。
    14 日まで広げても ``stale_min_cards`` に届かない場合はそれ以上は広げず、
    14 日窓の結果をそのまま返す (空を許容する — 無限に広げて明らかに古い
    値上げ前の「割引」を出し続けるほうが害が大きい)。

    records は ``_dedupe_by_asin`` を含む ``build_deals`` を複数回呼ぶために
    list 化して使い回す (Iterable が使い捨てジェネレータの場合に備える)。

    戻り値は ``(items, drops, used_stale_days)``。``used_stale_days`` は
    実際に採用された window (呼び出し側が manifest / log に記録して、
    「どの窓で作られたページなのか」を後から追跡できるようにする)。
    """
    records_list = list(records)
    ladder = sorted({stale_days, *(w for w in _STALE_FALLBACK_LADDER_ANCHORS if w >= stale_days)})

    items: list[ArticleRecord] = []
    drops: dict[str, int] = {}
    used_stale_days = ladder[0]
    for window in ladder:
        items, drops = build_deals(
            records_list,
            min_ivs=min_ivs,
            min_savings=min_savings,
            stale_days=window,
            top_n=top_n,
            now=now,
        )
        used_stale_days = window
        if len(items) >= stale_min_cards:
            break
        logger.info(
            "deals stale window %dd yielded %d cards (< stale_min_cards=%d)%s",
            window, len(items), stale_min_cards,
            "" if window == ladder[-1] else "; widening",
        )

    return items, drops, used_stale_days


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _record_to_payload_common(rec: ArticleRecord, rank: int) -> dict[str, Any]:
    payload = {
        "rank": rank,
        "asin": rec.asin,
        "slug": rec.slug,
        # URL 構造は #511 で /posts/{slug}/ -> /products/{asin}/ へ移行済み。
        # build_post.py 側は一貫して /products/{asin.lower()}/ を使っており
        # (build_post.py:407 等)、ここが legacy /posts/ のままだと内部リンクが
        # 全件 301 (Hugo aliases) 経由になり、記事削除時は alias ごと消えて
        # 404 化する (#3364)。
        "url_internal": f"/products/{rec.asin.lower()}/",  # Hugo lowercases URLs
        "name": rec.name,
        "image": rec.image,
        "ivs_100": rec.ivs_100,
        "ivs_score": rec.ivs_score,
        "best_price": rec.best_price,
        "best_platform": rec.best_platform,
        "amazon_url": rec.amazon_url,
        "age_min_months": rec.age_min_months,
    }
    if rec.ivs_axes:
        payload["ivs_axes"] = rec.ivs_axes
    return payload


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
    # #5225: 価格推移チャートはここで埋め込まない。build_price_sparks.py が
    # ASIN キーの hugo/data/price_sparks.json を作り、Hugo 側が partial
    # "price_spark_for.html" で引く。/deals/ だけでなく /cospa/ や hub、
    # product_card を使う全ページで同じチャートが出る。
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
    stale_days: int = 3,
    stale_min_cards: int = 8,
    now: datetime | None = None,
    raw_root: Path | None = None,
) -> dict[str, Any]:
    """Run the full pipeline. Returns the manifest dict for testability."""
    records = load_articles(articles_dir)
    attach_amazon_meta(records, per_asin_dir, raw_root=raw_root)

    cospa_bands = build_cospa_bands(
        records,
        min_ivs=min_ivs,
        top_n_per_band=top_n,
    )
    deals_items, deals_drops, deals_stale_window = build_deals_with_stale_fallback(
        records,
        min_ivs=min_ivs,
        min_savings=min_savings,
        stale_days=stale_days,
        stale_min_cards=stale_min_cards,
        top_n=top_n,
        now=now,
    )
    if deals_stale_window != stale_days:
        logger.info(
            "deals: widened stale window %dd -> %dd to reach stale_min_cards=%d (%d cards)",
            stale_days, deals_stale_window, stale_min_cards, len(deals_items),
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
        # stale_days: CLI/呼び出し元が指定した base window。実際に採用された
        # window (フォールバックで広がった場合はそれ以上) は stale_window_used。
        "stale_days": stale_days,
        "stale_min_cards": stale_min_cards,
        "stale_window_used": deals_stale_window,
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
            # #4007 follow-up 2: どの stale window で採用されたページなのかを
            # 常に manifest から追える形で残す (フォールバックで 3->7->14 と
            # 広がった場合、ページが古くなった原因を後から追跡できるように)。
            "stale_window_used": deals_stale_window,
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
    parser.add_argument(
        "--stale-days",
        type=int,
        default=3,
        help="#4007 follow-up 2: deals の base stale window (日)。既定 14 -> 3。"
             "採用カード数が --stale-min-cards を割ったら 7日 -> 14日 へ自動で"
             "広げる (build_deals_with_stale_fallback)。",
    )
    parser.add_argument(
        "--stale-min-cards",
        type=int,
        default=8,
        help="#4007 follow-up 2: stale window フォールバックの下限カード数。"
             "--top-n より小さい値を想定。",
    )
    parser.add_argument(
        "--raw-root",
        default="data/raw",
        type=Path,
        help="#4007 follow-up 1: data/raw/{rakuten,yahoo}_matched.json の親ディレクトリ。"
             "楽天/Yahoo 価格を build_post.py と同じ matched JSON で更新するために使う。",
    )
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
        stale_min_cards=args.stale_min_cards,
        raw_root=args.raw_root,
    )
    cospa_total = sum(manifest["cospa"].get("bands", {}).values())
    logger.info(
        "built: cospa=%d (across %d bands) deals=%d window=%dd (from %d articles)",
        cospa_total,
        len(manifest["cospa"].get("bands", {})),
        manifest["deals"]["count"],
        manifest["deals"]["stale_window_used"],
        manifest["articles_loaded"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


