"""Build Hugo post markdown from article JSON + enrichment + seo (おもちゃいろ v4).

For each ``data/articles/{slug}.json`` (excluding ``.enrichment.json`` /
``.seo.json`` / ``.quality.json`` siblings), merge the optional STAGE-2 and
STAGE-3 outputs and render ``hugo/content/posts/{slug}.md``.

Frontmatter is populated from STAGE-3 SEO data when available (optimized title /
meta description / jsonld / breadcrumbs), falling back to STAGE-1 fields.

Usage:
    python scripts/build_post.py
    python scripts/build_post.py --src data/articles/ --dst hugo/content/posts/
    python scripts/build_post.py --min-score 70   # mark below-threshold as draft
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import urllib.parse
from datetime import datetime
from typing import Any

import frontmatter
import jinja2

from brand_normalizer import normalize as normalize_brand
from score_calculator import calculate as calculate_score


SUFFIX_SKIP = (".enrichment", ".seo", ".quality")


def _sync_ivs_for_render(data: dict[str, Any]) -> None:
    """テンプレ参照用に product.ivs_detail を新スコアで上書きする。
    本文の IVS 総合/知育効果/長く遊べる/安全性/コスパ と加減点根拠を
    score_calculator の結果と同期させる (frontmatter とのズレ防止)。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    raw_brand = product.get("brand") if product else None
    if not (product and raw_brand):
        return
    nb = normalize_brand(raw_brand)
    sr = calculate_score(data, nb, asin=product.get("asin"))
    bd = sr.breakdown
    product["ivs_score"] = sr.ivs_score
    ivs = product.setdefault("ivs_detail", {})
    ivs["total_100"] = sr.total_100
    # 4 軸 (/5 表示) は 6 要素から再導出。
    # 「玩具である以上 0 は不当」のため 2.0-5.0 にスケール (中央 3.5)。
    # 式: axis = 2.0 + (raw / max) * 3.0  -> raw 0->2.0, mid->3.5, max->5.0
    def _scale(raw: float, max_v: float) -> float:
        return round(2.0 + (raw / max_v) * 3.0, 1)

    ivs["education"] = _scale(bd["edu_value"], 15)
    ivs["safety"] = _scale(bd["safety_cert"], 10)
    ivs["cost_performance"] = _scale(bd["price_value"], 15)
    # 長く遊べる = brand_tier (max 25) + media_exposure (max 15) を合算正規化
    longevity_norm = (bd["brand_tier"] / 25 + bd["media_exposure"] / 15) / 2
    ivs["longevity"] = round(2.0 + longevity_norm * 3.0, 1)
    ivs["score_rationale"] = [
        {"factor": "ブランド信頼度", "delta": f"+{bd['brand_tier']}/25", "reason": sr.rationale[0]},
        {"factor": "安全認証", "delta": f"+{bd['safety_cert']}/10", "reason": sr.rationale[1]},
        {"factor": "対象年齢", "delta": f"+{bd['age_fit']}/10", "reason": sr.rationale[2]},
        {"factor": "知育価値", "delta": f"+{bd['edu_value']}/15", "reason": sr.rationale[3]},
        {"factor": "メディア露出", "delta": f"+{bd['media_exposure']}/15", "reason": sr.rationale[4]},
        {"factor": "正規流通", "delta": f"+{bd['multi_market']}/10", "reason": sr.rationale[5]},
        {"factor": "コスパ", "delta": f"+{bd['price_value']}/15", "reason": sr.rationale[6]},
    ]
    # β テンプレ v5.1: レーダー軸 (上=コスパ / 下=長く遊べる / 左=知育 / 右=安全) を
    # SVG 用座標で事前計算する。viewBox 400x280, 中心 (200,140), 半径上限 90。
    # v5.0 比で横方向を大きく取り、軸ラベル「📚 知育効果 3.8」「🛡️ 安全性 4.4」等の
    # 末尾の値併記がクリップしないようにする。
    cx, cy, rmax = 200.0, 140.0, 90.0
    axes = [
        ("cost", ivs["cost_performance"], cx, cy - rmax),       # 上
        ("safety", ivs["safety"], cx + rmax, cy),               # 右
        ("longevity", ivs["longevity"], cx, cy + rmax),         # 下
        ("education", ivs["education"], cx - rmax, cy),         # 左
    ]
    points = []
    for _key, val, ex, ey in axes:
        ratio = max(0.0, min(1.0, float(val) / 5.0))
        px = round(cx + (ex - cx) * ratio, 1)
        py = round(cy + (ey - cy) * ratio, 1)
        points.append(f"{px},{py}")
    ivs["radar_points"] = " ".join(points)
    # 総合スコアの棒グラフ (1 本) は total_100 をそのまま % として使う。
    # 4 軸ごとの bar_*_pct は v5.1 でレーダー一元化に統合し撤去。
    # Amazon レビューアンカー URL (CTA 用)
    asin = product.get("asin")
    if asin:
        product["amazon_review_url"] = (
            f"https://www.amazon.co.jp/dp/{asin}/?tag=zefiransesu-22#customerReviews"
        )

BADGE_FIELDS = ("availability", "loyalty_points", "savings_percentage")

ENRICHMENT_KEYS = (
    "review_summary",
    "use_case_scenarios",
    "competitive_position",
    "gift_reaction_prediction",
    "expert_take",
)

SEO_KEYS = (
    "title_variants",
    "meta_description_optimized",
    "h1_recommendation",
    "h2_recommendations",
    "faq_extended",
    "breadcrumbs",
    "jsonld",
    "internal_link_suggestions",
)


def _load_optional_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ! skip malformed {path.name}: {e}")
        return {}


def _merge(data: dict[str, Any], extra: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in extra and key not in data:
            data[key] = extra[key]


def _load_raw_amazon_index(raw_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Return {asin: item} from data/raw/amazon.json, or {} if absent/malformed.

    Used to back-fill availability / loyalty_points / savings_percentage into
    article JSON when the upstream generator (Jules) did not include them.
    """
    if not raw_path.exists():
        return {}
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {it.get("asin"): it for it in raw.get("items", []) if it.get("asin")}


def _load_per_asin_competitors(per_asin_root: pathlib.Path, asin: str) -> list[dict[str, Any]]:
    """Return the API-fetched competitor list for ``asin`` (possibly empty).

    Written by fetch_amazon.py as
    ``data/raw/per_asin/<ASIN>/competitors.json`` with shape
    ``{asin, fetched_at, competitors: [{asin, name, image, price, url, features}]}``.
    Every entry has a verified ASIN, image, and affiliate URL — so they can
    replace Jules-hallucinated ``competitive_analysis[]`` items wholesale.
    """
    p = per_asin_root / asin / "competitors.json"
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = payload.get("competitors") if isinstance(payload, dict) else None
    return [c for c in items if isinstance(c, dict) and c.get("asin")] if isinstance(items, list) else []


def _price_comparison_label(target_price: int, competitor_price: int) -> str:
    """競合の絶対価格と本品との差額を一体表示する。
    差額のみ出すと「2,087円高い」だけが残り、本品/競合の基準価格が見えず
    読者が判断できない問題があったため、必ず競合の絶対価格を先に出す。
    """
    if not competitor_price:
        return ""
    abs_yen = f"¥{competitor_price:,}"
    if not target_price:
        return abs_yen
    diff = competitor_price - target_price
    if abs(diff) < 100:
        return f"{abs_yen}（本品とほぼ同価格）"
    diff_yen = f"{abs(diff):,}円"
    rel = f"本品より{diff_yen}安い" if diff < 0 else f"本品より{diff_yen}高い"
    return f"{abs_yen}（{rel}）"


_COMPETITOR_NAME_MAX = 38
_COMPETITOR_FEATURE_MAX = 60
_COMPETITOR_TOP_N = 3


def _parse_age_min_months(raw: Any) -> int:
    """年齢文字列から最小月齢（months）を数値でパースする。"""
    if not raw:
        return 0
    raw_str = str(raw).strip()
    if not raw_str:
        return 0
    
    # 1.5歳半、18ヶ月などの特異パターンを優先判定
    if re.search(r"1\.5|1歳半|1歳6ヶ月|18ヶ月", raw_str):
        return 18
    
    # 範囲パターン: e.g. "6ヶ月〜2歳", "3歳〜7歳"
    m_range = re.search(r"(\d+)\s*(歳|才|ヶ月)\s*[〜~\-]", raw_str)
    if m_range:
        n, u = int(m_range.group(1)), m_range.group(2)
        return n if u == "ヶ月" else n * 12
        
    # 下限のみ: e.g. "6歳以上", "3歳〜", "12ヶ月から"
    m_min = re.search(r"(\d+)\s*(歳|才|ヶ月)\s*(?:〜|~|から|以上|\+)", raw_str)
    if m_min:
        n, u = int(m_min.group(1)), m_min.group(2)
        return n if u == "ヶ月" else n * 12

    # 単一: e.g. "3歳"
    m_single = re.search(r"(\d+)\s*(歳|才|ヶ月)", raw_str)
    if m_single:
        n, u = int(m_single.group(1)), m_single.group(2)
        return n if u == "ヶ月" else n * 12

    # 単なる数値
    m_num = re.search(r"(\d+)", raw_str)
    if m_num:
        return int(m_num.group(1)) * 12
        
    return 0


_TRAILING_BRACKET_RE = re.compile(r"\s*[（(\[【][^）)\]】]*[）)\]】]\s*$")


def _shrink_competitor_name(name: str) -> str:
    """カード見出し用に商品名を縮める。Amazon タイトルは長すぎてカードが
    崩れるため一定文字数で省略。末尾の括弧コピー (例 ``【...優秀賞】``
    ``(対象年齢:1歳半以上)``) は情報量が低いので 1 ブロックずつ剥がす。
    """
    if not name:
        return ""
    s = name.strip()
    while True:
        stripped = _TRAILING_BRACKET_RE.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    if len(s) <= _COMPETITOR_NAME_MAX:
        return s
    return s[: _COMPETITOR_NAME_MAX - 1].rstrip("、。・,. ") + "…"


def _shrink_competitor_feature(text: str) -> str:
    """カード内の feature_comparison を 1 行プレビューに丸める。"""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    # 「特徴：」プレフィックスは付けたまま受け取るのでそのまま縮める
    if len(s) <= _COMPETITOR_FEATURE_MAX:
        return s
    return s[: _COMPETITOR_FEATURE_MAX - 1].rstrip("、。・,. ") + "…"


def _build_asin_to_slug_map(src_path: pathlib.Path) -> dict[str, str]:
    """Return {asin: slug} for every article JSON under ``src_path``.

    Used so competitor cards can deep-link to an existing internal article
    when the competitor ASIN already has dedicated coverage on the site.
    """
    mapping: dict[str, str] = {}
    if not src_path.exists():
        return mapping
    for f in src_path.glob("*.json"):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = meta.get("slug") or f.stem
        product = meta.get("product") if isinstance(meta.get("product"), dict) else None
        asin = product.get("asin") if product else None
        if not asin:
            m = re.search(r"-(B0[A-Z0-9]{8})$", f.stem)
            if m:
                asin = m.group(1)
        if not asin:
            continue
        mapping.setdefault(asin, slug)
    return mapping


def _site_base_path(config_path: pathlib.Path) -> str:
    """Extract the path component of ``baseURL`` from hugo/config.toml.

    Returns e.g. ``/amazon`` for ``https://omochairo.github.io/amazon/``,
    or ``""`` when the site is served at the host root. Hugo does not
    rewrite raw <a href> URLs emitted from our jinja template, so the
    GitHub-Pages subpath has to be baked in here.
    """
    if not config_path.exists():
        return ""
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("baseurl"):
                continue
            m = re.search(r"['\"]([^'\"]+)['\"]", stripped)
            if not m:
                return ""
            from urllib.parse import urlparse
            return urlparse(m.group(1)).path.rstrip("/")
    except OSError:
        return ""
    return ""


def _attach_internal_links(
    data: dict[str, Any],
    asin_to_slug: dict[str, str],
    site_base_path: str,
) -> None:
    """Mark each competitor entry with ``internal_url`` when we already
    publish an article for that ASIN. The current article's own ASIN is
    excluded so a card never self-links."""
    ca = data.get("competitive_analysis")
    if not isinstance(ca, list) or not asin_to_slug:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    self_asin = product.get("asin") if product else None
    for c in ca:
        if not isinstance(c, dict):
            continue
        asin = c.get("asin")
        if not asin or asin == self_asin:
            continue
        slug = asin_to_slug.get(asin)
        if slug:
            # URL structure moved from /posts/{slug}/ to /products/{asin}/
            # (See #511). Hugo aliases handle 301 redirects for the old paths.
            c["internal_url"] = f"{site_base_path}/products/{asin.lower()}/"


def _override_competitive_analysis(
    data: dict[str, Any],
    per_asin_root: pathlib.Path,
) -> None:
    """If we have API-fetched competitors for this article's ASIN, replace
    ``competitive_analysis`` with entries that always resolve to a real listing
    (verified ASIN, real image URL, real affiliate URL).

    The Jules-authored ``feature_comparison`` / ``differentiators`` text is
    dropped — those routinely paired with fabricated ASINs, so trying to
    fuzzy-match them onto real competitors is worse than starting clean.
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return
    competitors = _load_per_asin_competitors(per_asin_root, asin)
    if not competitors:
        # Jules routinely pairs a real-sounding ``name`` with a fabricated
        # ``asin`` (sometimes one that happens to belong to a different
        # article on this very site, which would otherwise be turned into a
        # misleading internal link). Strip the ASIN entirely when we have
        # no API-verified replacement — the card still renders its text
        # block, but the thumbnail / Amazon CTA / internal-link button all
        # disappear via the ``c.asin AND (c.image OR c.internal_url)`` gate.
        ca = data.get("competitive_analysis")
        if isinstance(ca, list):
            for c in ca:
                if isinstance(c, dict):
                    c.pop("asin", None)
        return
    target_price = 0
    amazon_price = (product.get("prices") or {}).get("amazon") or {}
    if isinstance(amazon_price, dict):
        try:
            target_price = int(amazon_price.get("price") or 0)
        except (TypeError, ValueError):
            target_price = 0

    new_entries: list[dict[str, Any]] = []
    for c in competitors[:_COMPETITOR_TOP_N]:
        try:
            cp = int(c.get("price") or 0)
        except (TypeError, ValueError):
            cp = 0
        raw_features = [f for f in (c.get("features") or []) if isinstance(f, str) and f.strip()]
        if raw_features:
            feature_line = _shrink_competitor_feature(f"特徴：{raw_features[0]}")
            feature_comparison = [feature_line] if feature_line else []
        else:
            feature_comparison = []
        new_entries.append({
            "asin": c["asin"],
            "name": _shrink_competitor_name(c.get("name") or ""),
            "image": c.get("image") or "",
            "url": c.get("url") or f"https://www.amazon.co.jp/dp/{c['asin']}/",
            "price": cp,
            "price_comparison": _price_comparison_label(target_price, cp),
            "feature_comparison": feature_comparison,
            "differentiators": [],
        })
    data["competitive_analysis"] = new_entries


def _load_per_asin_items(
    per_asin_root: pathlib.Path,
    asin: str,
    filename: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` items from ``data/raw/per_asin/<ASIN>/<filename>``.

    Used as a fallback for the Jules-authored ``news`` / ``books`` /
    ``youtube_embeds`` fields when they come back empty. File shape:
    ``{"items": [...]}``.
    """
    p = per_asin_root / asin / filename
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [
        it for it in items
        if isinstance(it, dict) and it.get("title") and it.get("url")
    ][:limit]


def _fallback_news_books(data: dict[str, Any], per_asin_root: pathlib.Path) -> None:
    """When the article JSON's ``news``/``books`` field is missing or empty,
    populate it from the per-ASIN snapshots written by fetch_yahoo_news.py and
    fetch_books.py. Non-empty Jules-authored values are preserved."""
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return
    for key, filename in (("news", "news.json"), ("books", "books.json")):
        if data.get(key):
            continue
        items = _load_per_asin_items(per_asin_root, asin, filename)
        if items:
            data[key] = items


def _fallback_youtube_embeds(
    data: dict[str, Any],
    per_asin_root: pathlib.Path,
    limit: int = 3,
) -> None:
    """When the article JSON's ``youtube_embeds`` field is missing or empty,
    populate it from ``data/raw/per_asin/<ASIN>/youtube.json`` (written by
    filter_raw_per_asin.py on top of fetch_youtube.py's pool). Items are
    passed through as-is; the post template reads ``vid.url`` (in
    ``youtube.com/watch?v=<id>`` format) and ``vid.title`` to build the
    iframe — no ``embed_html`` synthesis needed here. Non-empty
    Jules-authored values are preserved."""
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return
    if data.get("youtube_embeds"):
        return
    items = _load_per_asin_items(per_asin_root, asin, "youtube.json", limit=limit)
    if items:
        data["youtube_embeds"] = items


# UTM パラメータ: GA4 で omcha.jp 側が本サイト (amazon サブサイト) からの
# 流入を計測できるようにする。utm_content には ASIN を入れて、どの商品記事
# からの遷移かを記事単位で区別する。
_OMCHA_UTM_BASE = (
    "utm_source=omochairo-amazon"
    "&utm_medium=referral"
    "&utm_campaign=related_card"
)


def _append_omcha_utm(url: str, asin: str | None) -> str:
    """Append UTM tracking params to an omcha.jp URL. Idempotent — URLs that
    already carry ``utm_source`` are returned untouched."""
    if not url or "utm_source=" in url:
        return url
    sep = "&" if "?" in url else "?"
    suffix = _OMCHA_UTM_BASE
    if asin:
        suffix = f"{suffix}&utm_content={asin}"
    return f"{url}{sep}{suffix}"


def _attach_omcha_related(data: dict[str, Any], per_asin_root: pathlib.Path) -> None:
    """``data["omcha_related"]`` を per-ASIN キャッシュから埋める (read-only)。

    キャッシュ自体の生成は `scripts/fetch_omcha_related.py` が 01-fetch flow で
    行う (50/run × 7日 stale-first cycle、PR #486 と同型)。build_post は
    `per_asin/<ASIN>/omcha_related.json` を読んで UTM 装飾するだけ。
    Jules-authored な `omcha_related` がある場合 (今は未使用、将来の予約) はそれを優先。
    キャッシュ未存在の新規記事は no-op (次回 fetch_omcha_related サイクルでカードが出る)。

    Issue #674 でこのリファクタを実施。旧設計の問題:
    - per_asin dir に書く 24h TTL キャッシュが untracked 汚染を生んでいた
    - build_post が描画ループ内で 218 ASIN 分 omcha.jp を叩いていた
    - score_calculator._omcha_top_score() が「ファイル無→0 点」race を持っていた
    """
    if data.get("omcha_related"):
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return
    cache_path = per_asin_root / asin / "omcha_related.json"
    if not cache_path.exists():
        return
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = cached.get("items") if isinstance(cached, dict) else None
    if not isinstance(items, list) or not items:
        return
    # 旧キャッシュ (thumbnail フィールド無し) は古いスキーマなのでスキップ。
    # 次回 fetch_omcha_related サイクルで新スキーマに置き換わる。
    if "thumbnail" not in items[0]:
        return
    decorated: list[dict[str, Any]] = []
    for it in items[:3]:
        new_it = dict(it)
        new_it["url"] = _append_omcha_utm(new_it.get("url", ""), asin)
        decorated.append(new_it)
    data["omcha_related"] = decorated


def _load_per_asin_amazon(per_asin_root: pathlib.Path, asin: str) -> dict[str, Any] | None:
    """Return the per-ASIN amazon snapshot dict, or None if absent/malformed.

    Snapshots are written by fetch_amazon.py as
    ``data/raw/per_asin/<ASIN>/amazon.json`` with shape ``{asin, fetched_at, item}``.
    Older snapshots may store the item dict at the root — both shapes are tolerated.
    """
    p = per_asin_root / asin / "amazon.json"
    if not p.exists():
        return None
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(snap, dict):
        return None
    item = snap.get("item") if isinstance(snap.get("item"), dict) else snap
    return item if isinstance(item, dict) else None


def _backfill_amazon_badges(
    data: dict[str, Any],
    raw_index: dict[str, dict[str, Any]],
    per_asin_root: pathlib.Path,
) -> None:
    """If prices.amazon is missing badge fields, copy them from the latest
    raw/amazon.json first, then fall back to the per-ASIN snapshot under
    data/raw/per_asin/<ASIN>/amazon.json. Existing values in the article win."""
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return

    sources: list[dict[str, Any]] = []
    raw_item = raw_index.get(asin)
    if raw_item:
        sources.append(raw_item)
    snap_item = _load_per_asin_amazon(per_asin_root, asin)
    if snap_item:
        sources.append(snap_item)
    if not sources:
        return

    prices = product.setdefault("prices", {})
    amazon = prices.setdefault("amazon", {})
    for field in BADGE_FIELDS:
        if amazon.get(field):
            continue
        for src in sources:
            val = src.get(field)
            if val:
                amazon[field] = val
                break


# 楽天/Yahoo cross-search 結果を本文の価格グリッドに流し込むときに、
# 検索ヒットしただけで実商品とかけ離れた item (ふるさと納税の高額品など) を
# 弾くためのガード閾値。Amazon 価格を anchor にする。
_MARKET_PRICE_BAND_LOW = 0.5   # Amazon 価格の 50% 未満は除外
_MARKET_PRICE_BAND_HIGH = 2.0  # Amazon 価格の 200% 超は除外
# verified=False の existing として keep する場合でも、Amazon の 3.0x 超 / 1/3 未満
# の極端な乖離は別商品確定として丸ごと破棄し、検索フォールバックのみ残す。
# 例: B0F4X462WH (amazon 1579円) yahoo_matched が 14395 円 (9.12x) で Jules が同 URL
#     を埋めていたケース。確度低 badge では「クリックすれば確認できる」と誤読される
#     ので、リンク自体を消して「Yahoo!で検索 →」ボタンに置き換える。
_MARKET_PRICE_BAND_EXTREME = 3.0
# coverage ratio (hits / len(meaningful))。これ未満は borderline 扱いで
# verified=False に格下げ → ※確度低 badge + 検索 fallback 表示。
# 例: kw='Hape ビーズコインドロップス E0328' vs title='Hape ビーズコインドロップス
# E0327' は 2/3=0.67 で borderline (異モデル番号) として捕捉される。
_MARKET_COVERAGE_RATIO = 0.7
# search_keyword を title overlap 判定するときに、汎用すぎて根拠にならない語
_MARKET_GENERIC_TOKENS = frozenset({
    "おもちゃ", "知育玩具", "プレゼント", "誕生日", "ギフト",
    "木製", "木のおもちゃ", "セット", "玩具",
})


def _load_matched_index(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """data/raw/{rakuten,yahoo}_matched.json を ``{asin: item}`` に展開。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for it in data.get("items", []):
        if not isinstance(it, dict):
            continue
        asin = it.get("matched_asin") or it.get("asin")
        if asin:
            index[asin] = it
    return index


def _matched_passes_quality(matched: dict[str, Any], amazon_price: int) -> bool:
    """Phase 2 quality gate: 価格帯と検索語タイトル overlap で誤マッチを弾く。

    - Amazon 価格 (>0) を anchor に [0.5x, 2.0x] 帯外を除外 (ふるさと納税対策)。
    - search_keyword のうち汎用語を除いた meaningful token が、matched title に
      閾値以上一致しているかを確認 (median band 選出後の無関係 hit 除外)。
    """
    title = matched.get("title") or ""
    try:
        price = int(matched.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if not title or price <= 0:
        return False

    if amazon_price > 0:
        if price < amazon_price * _MARKET_PRICE_BAND_LOW:
            return False
        if price > amazon_price * _MARKET_PRICE_BAND_HIGH:
            return False

    kw = matched.get("search_keyword") or ""
    kw_tokens = [t for t in re.split(r"\s+", kw) if len(t) >= 2]
    meaningful = [t for t in kw_tokens if t not in _MARKET_GENERIC_TOKENS]
    if not meaningful:
        return True  # 区別語が無ければ cross-search 側 median band の選出を尊重
    hits = sum(1 for t in meaningful if t in title)
    threshold = 2 if len(meaningful) >= 2 else 1
    if hits < threshold:
        return False
    # 絶対 hits 数を満たしても、meaningful 全体に占める割合が低いマッチは
    # シリーズ違い/別モデルの誤マッチが多いため verified=False に格下げ。
    if hits / len(meaningful) < _MARKET_COVERAGE_RATIO:
        return False
    return True


_SEARCH_URL_BUILDERS = {
    "rakuten": lambda q: f"https://search.rakuten.co.jp/search/mall/{urllib.parse.quote(q)}/",
    "yahoo": lambda q: f"https://shopping.yahoo.co.jp/search?p={urllib.parse.quote(q)}",
}


def _attach_market_prices(
    data: dict[str, Any],
    rakuten_index: dict[str, dict[str, Any]],
    yahoo_index: dict[str, dict[str, Any]],
) -> None:
    """``product.prices.{rakuten,yahoo}`` を matched JSON で検証/上書きする。

    優先順位:
      1. matched JSON に entry があり Phase 2 ガード (価格帯 + 検索語 overlap)
         を通過 → ``verified=True`` で **matched データを採用** (Jules の値を
         上書き)。これが「決定論的に同一商品と判定済み」の唯一の正規ソース。
      2. matched は無いが Jules が price/url を埋めている → そのまま残す。
         ただし ``verified=False`` と検索フォールバック ``search_url`` を付与
         し、テンプレ側で「※確度低」バッジ + 検索リンクを表示する。
      3. matched も Jules 由来も無い → 既存挙動 (テンプレが「取り扱い確認」)。

    最後に ``product.best_price`` / ``product.best_platform`` を全プラット
    フォーム最安で再計算する。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return

    prices = product.setdefault("prices", {})
    amazon_entry = prices.get("amazon") if isinstance(prices.get("amazon"), dict) else None
    amazon_price = 0
    if amazon_entry:
        try:
            amazon_price = int(amazon_entry.get("price") or 0)
        except (TypeError, ValueError):
            amazon_price = 0

    product_name = product.get("name") or ""

    for key, index in (("rakuten", rakuten_index), ("yahoo", yahoo_index)):
        existing = prices.get(key) if isinstance(prices.get(key), dict) else None
        matched = index.get(asin)
        matched_ok = bool(matched) and _matched_passes_quality(matched, amazon_price)

        if matched_ok:
            try:
                mprice = int(matched.get("price") or 0)
            except (TypeError, ValueError):
                mprice = 0
            prices[key] = {
                "price": mprice,
                "url": matched.get("url") or "",
                "title": matched.get("title") or "",
                "is_search": False,
                "verified": True,
                "availability": matched.get("availability"),
                "free_shipping": matched.get("free_shipping"),
                "asuraku": matched.get("asuraku"),
            }
            continue

        existing_price = 0
        if existing:
            try:
                existing_price = int(existing.get("price") or 0)
            except (TypeError, ValueError):
                existing_price = 0
        existing_extreme = (
            amazon_price > 0 and existing_price > 0 and (
                existing_price > amazon_price * _MARKET_PRICE_BAND_EXTREME
                or existing_price < amazon_price / _MARKET_PRICE_BAND_EXTREME
            )
        )

        if existing and not existing_extreme and (existing.get("price") or existing.get("url")):
            # Jules-supplied data with no deterministic cross-check. Keep it
            # so we don't lose the (often correct, sometimes wrong) link, but
            # flag it as unverified so the template can render a 確度低 badge
            # plus a fallback search URL.
            existing["verified"] = False
            existing.setdefault("is_search", False)
            if product_name and not existing.get("search_url"):
                builder = _SEARCH_URL_BUILDERS.get(key)
                if builder:
                    existing["search_url"] = builder(product_name)
            prices[key] = existing
            continue

        # Final fallback: matched failed, and either no Jules-supplied data or
        # an extreme price outlier we just discarded. Render a search-mode
        # entry so the template shows a 楽天/Yahoo!で検索 button pointing at
        # the product name instead of an empty 取り扱い確認 / リンクなし cell.
        if product_name:
            builder = _SEARCH_URL_BUILDERS.get(key)
            if builder:
                prices[key] = {
                    "price": 0,
                    "url": builder(product_name),
                    "is_search": True,
                }

    _recompute_best_price(product)


def _recompute_best_price(product: dict[str, Any]) -> None:
    """全プラットフォーム最安を ``product.best_price`` / ``best_platform`` に反映。"""
    prices = product.get("prices")
    if not isinstance(prices, dict):
        return
    labels = (("amazon", "Amazon"), ("rakuten", "楽天市場"), ("yahoo", "Yahoo!ショッピング"))
    candidates: list[tuple[int, str]] = []
    for key, label in labels:
        entry = prices.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            p = int(entry.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p > 0:
            candidates.append((p, label))
    if not candidates:
        return
    best = min(candidates, key=lambda x: x[0])
    product["best_price"] = best[0]
    product["best_platform"] = best[1]


def _backfill_product_images(
    data: dict[str, Any],
    raw_index: dict[str, dict[str, Any]],
    per_asin_root: pathlib.Path,
) -> None:
    """Attach ``product.images`` (variant gallery) from raw/amazon.json
    or the per-ASIN snapshot. Jules-authored JSON only carries a single
    ``product.image``; PA-API ``images.variants.large`` gives us extra
    angles which the template renders as a thumbnail strip.

    Existing ``product.images`` (e.g. a future Jules schema bump) wins.
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return
    existing = product.get("images")
    if isinstance(existing, list) and existing:
        return

    images: list[str] = []
    primary = product.get("image")
    if isinstance(primary, str) and primary:
        images.append(primary)
    for src in (raw_index.get(asin), _load_per_asin_amazon(per_asin_root, asin)):
        if not isinstance(src, dict):
            continue
        for u in src.get("images") or []:
            if isinstance(u, str) and u and u not in images:
                images.append(u)
        if len(images) > 1:
            break
    if len(images) > 1:
        product["images"] = images


def _fill_jsonld(data: dict[str, Any]) -> None:
    # 既存の jsonld を取得、なければ初期化
    jsonld = data.setdefault("jsonld", {})
    if not isinstance(jsonld, dict):
        data["jsonld"] = {}
        jsonld = data["jsonld"]

    product_data = data.get("product") or {}
    
    # 1. Product スキーマの動的生成
    if "product" not in jsonld or not isinstance(jsonld["product"], dict):
        jsonld["product"] = {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": product_data.get("name", data.get("title", "")),
            "image": product_data.get("image", ""),
            "description": data.get("meta_description", ""),
        }
        
    product_ld = jsonld["product"]
    
    # brand の補正
    raw_brand = product_data.get("brand")
    if raw_brand and "brand" not in product_ld:
        product_ld["brand"] = {
            "@type": "Brand",
            "name": raw_brand
        }

    # 1. 評価データ (AggregateRating) の注入
    review_summary = data.get("review_summary")
    avg_rating = None
    review_count = None
    if isinstance(review_summary, dict):
        avg_rating = review_summary.get("avg_rating")
        review_count = review_summary.get("review_count")
    
    if avg_rating is None:
        avg_rating = product_data.get("ivs_score")
        if avg_rating is None:
            avg_rating = 4.0

    # Googleの構造化データガイドライン違反（虚偽評価ペナルティ）を回避するため、
    # 独自スコア評価として reviewCount: "1" に固定し、descriptionを付与する。
    review_count = "1"
        
    product_ld["aggregateRating"] = {
        "@type": "AggregateRating",
        "ratingValue": str(avg_rating),
        "reviewCount": str(review_count),
        "description": "おもちゃロボによる知育スコア評価",
    }

    # 2. 価格帯 (AggregateOffer) の更新
    prices = product_data.get("prices")
    valid_prices = []
    if isinstance(prices, dict):
        for key in ("amazon", "rakuten", "yahoo"):
            entry = prices.get(key)
            if isinstance(entry, dict):
                try:
                    p = int(entry.get("price") or 0)
                    if p > 0 and not entry.get("is_search", False):
                        valid_prices.append(p)
                except (TypeError, ValueError):
                    pass
    
    if valid_prices:
        low_price = min(valid_prices)
        high_price = max(valid_prices)
        offer_count = len(valid_prices)
    else:
        best_price = product_data.get("best_price")
        try:
            bp = int(best_price or 0)
        except (TypeError, ValueError):
            bp = 0
        if bp > 0:
            low_price = bp
            high_price = bp
            offer_count = 1
        else:
            low_price = 0
            high_price = 0
            offer_count = 0

    if offer_count > 0:
        offers = product_ld.setdefault("offers", {})
        offers.update({
            "@type": "AggregateOffer",
            "lowPrice": str(low_price),
            "highPrice": str(high_price),
            "priceCurrency": "JPY",
            "offerCount": str(offer_count),
        })

    # 2. FAQ スキーマの動的生成
    faq = data.get("faq_extended") or data.get("faq") or []
    if faq:
        if "faq" not in jsonld or not isinstance(jsonld["faq"], dict):
            jsonld["faq"] = {
                "@context": "https://schema.org/",
                "@type": "FAQPage",
                "mainEntity": []
            }
        
        if isinstance(jsonld.get("faq"), dict):
            jsonld["faq"]["mainEntity"] = [
                {
                    "@type": "Question",
                    "name": q.get("question", ""),
                    "acceptedAnswer": {"@type": "Answer", "text": q.get("answer", "")},
                }
                for q in faq
            ]

    # 3. Breadcrumb スキーマの動的生成
    breadcrumbs = data.get("breadcrumbs") or []
    if breadcrumbs:
        if "breadcrumb" not in jsonld or not isinstance(jsonld["breadcrumb"], dict):
            jsonld["breadcrumb"] = {
                "@context": "https://schema.org/",
                "@type": "BreadcrumbList",
                "itemListElement": []
            }
        
        if isinstance(jsonld.get("breadcrumb"), dict):
            jsonld["breadcrumb"]["itemListElement"] = [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "name": b.get("name", ""),
                    "item": b.get("url", ""),
                }
                for i, b in enumerate(breadcrumbs)
            ]




def _load_git_history(src_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """git log を一括実行し、各記事 JSON ファイルの更新履歴を取得する。
    返り値の形式: { "2026-05-11-B00I7JXEEA.json": { "lastmod": "ISO_DATE", "update_count": N } }
    """
    import subprocess
    history: dict[str, dict[str, Any]] = {}
    src_dir_str = str(src_path).replace("\\", "/")
    if not src_dir_str.endswith("/"):
        src_dir_str += "/"

    try:
        # git log を実行してコミット時のファイル変更リストを取得
        res = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:COMMIT:%cI"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True
        )
        lines = res.stdout.splitlines()
    except Exception as e:
        print(f"  ! Failed to retrieve git log: {e}. Fallback to file timestamps.")
        return {}

    current_commit_date = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT:"):
            current_commit_date = line[7:]
        else:
            normalized_line = line.replace("\\", "/")
            if normalized_line.startswith(src_dir_str) and normalized_line.endswith(".json"):
                basename = normalized_line[len(src_dir_str):]
                if basename.endswith(SUFFIX_SKIP):
                    continue
                if basename not in history:
                    history[basename] = {
                        "lastmod": current_commit_date,
                        "update_count": 0
                    }
                history[basename]["update_count"] += 1
    return history


def _frontmatter_meta(
    data: dict[str, Any],
    slug: str,
    draft: bool,
    git_history: dict[str, dict[str, Any]],
    json_file_path: pathlib.Path,
) -> dict[str, Any]:
    title = data.get("title", "No Title")
    variants = data.get("title_variants") or []
    if variants and isinstance(variants[0], dict) and variants[0].get("title"):
        title = variants[0]["title"]
    description = data.get("meta_description_optimized") or data.get("meta_description", "")
    
    product = data.get("product") or {}
    asin = product.get("asin")
    if not asin:
        import re
        m = re.search(r"-(B0[A-Z0-9]{8})$", slug, flags=re.IGNORECASE)
        if m:
            asin = m.group(1)
        else:
            asin = slug

    # git_history から値を取得する
    json_filename = json_file_path.name
    history_entry = git_history.get(json_filename)
    
    if history_entry:
        lastmod = history_entry["lastmod"]
        update_count = history_entry["update_count"]
    else:
        # 暫定化: コミットがない場合（新規ファイル等）や Git が無効な場合はファイルの最終更新日時を使用
        try:
            mtime = json_file_path.stat().st_mtime
            lastmod = datetime.fromtimestamp(mtime).astimezone().isoformat()
        except Exception:
            lastmod = data.get("date", datetime.now().isoformat())
        update_count = 1

    meta: dict[str, Any] = {
        "title": title,
        "date": data.get("date", datetime.now().isoformat()),
        "lastmod": lastmod,
        "update_count": update_count,
        "tags": [t.rstrip(". ") for t in data.get("tags", []) if t],
        "slug": slug,
        "url": f"/products/{asin.lower()}/",
        "aliases": [f"/posts/{slug.lower()}/"],
        "draft": draft,
        "description": description,
        "keywords": data.get("keywords", []),
    }
    image_url = product.get("image") or ""
    if image_url:
        meta["product_image"] = image_url
    # 2026-05-15 (@J Phase 1): data/brand_taxonomy.yaml に基づくブランド正規化。
    # raw brand (Jules / API 取得時の表記ゆれ含む) → canonical 名に統一して
    # Hugo /brands/<canonical>/ ページで集約検索できるようにする。
    # 旧 UI 側ブラックリストはこちらに移譲済。
    raw_brand = product.get("brand")
    if raw_brand:
        nb = normalize_brand(raw_brand)
        meta["brand"] = nb.canonical
        meta["brand_raw"] = raw_brand
        meta["brand_tier"] = nb.tier
        meta["brand_region"] = nb.region
        meta["brand_match_type"] = nb.match_type
        if not nb.exclude_from_taxonomy:
            meta["brands"] = [nb.canonical]
    if product.get("name"):
        meta["product_name"] = product["name"]
    if product.get("age_range"):
        meta["age_range"] = product["age_range"]
    
    # 年齢フィルター用の最小月数メタデータを追加
    raw_age = ""
    if isinstance(product, dict):
        raw_age = (
            product.get("target_age")
            or product.get("age_range")
            or product.get("age")
            or product.get("age_band")
            or ""
        )
    if not raw_age and isinstance(data, dict):
        raw_age = (
            data.get("persona_fit", {}).get("age_range")
            or data.get("technical_specs", {}).get("age_range")
            or ""
        )
        if not raw_age and isinstance(data.get("technical_specs"), dict):
            other_specs = data["technical_specs"].get("other", [])
            for spec in other_specs:
                if isinstance(spec, str) and ("年齢" in spec or "才" in spec):
                    raw_age = spec
                    break
        if not raw_age and product and product.get("name_full"):
            name_full = product["name_full"]
            match = re.search(r"(\d+)\s*(歳|才|ヶ月|ヶ月以上|歳以上)", name_full)
            if match:
                raw_age = match.group(0)
                
    meta["age_min_months"] = _parse_age_min_months(raw_age)

    # 2026-05-15 (@J Phase 2): Jules の ivs_score を破棄し、6要素から論理再計算する。
    # Jules スコアはブランド信頼度を反映しないため (ノーブランド=4.7 等の不正)、
    # brand_tier(25) + safety(10) + age(10) + edu(15) + media(15) + market(10) + price(15)
    # = total_100 を score_calculator で算出して上書きする。
    # Jules の元値は ivs_score_jules に保持し、比較・デバッグに使う。
    jules_ivs = product.get("ivs_score")
    if raw_brand:
        sr = calculate_score(data, nb, asin=product.get("asin"))
        meta["ivs_score"] = sr.ivs_score
        meta["ivs_score_100"] = sr.total_100
        meta["score_breakdown"] = sr.breakdown
        if isinstance(jules_ivs, (int, float)):
            meta["ivs_score_jules"] = float(jules_ivs)
    else:
        # brand が空のレアケース: Jules の値を流用 (互換性)
        if isinstance(jules_ivs, (int, float)):
            meta["ivs_score"] = float(jules_ivs)
        ivs_detail = product.get("ivs_detail") or {}
        total_100 = ivs_detail.get("total_100")
        if isinstance(total_100, (int, float)):
            meta["ivs_score_100"] = int(total_100)
    if data.get("jsonld"):
        meta["jsonld"] = data["jsonld"]
    if data.get("breadcrumbs"):
        meta["breadcrumbs"] = data["breadcrumbs"]
        
    # 2026-05-21 (#516): 価格ソート用のメタデータ追加
    if product and isinstance(product.get("prices"), dict):
        prices = product["prices"]
        for key in ("amazon", "rakuten", "yahoo"):
            entry = prices.get(key)
            if isinstance(entry, dict):
                try:
                    price_val = int(entry.get("price") or 0)
                    if price_val > 0:
                        meta[f"price_{key}"] = price_val
                except (TypeError, ValueError):
                    pass

    return meta


def _quality_draft(slug: str, src_path: pathlib.Path, min_score: int) -> bool:
    if min_score <= 0:
        return False
    qpath = src_path / f"{slug}.quality.json"
    if not qpath.exists():
        return False
    try:
        q = json.loads(qpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    score = q.get("total_score", q.get("score", 0))
    return int(score) < min_score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--dst", default="hugo/content/posts/")
    parser.add_argument("--min-score", type=int, default=0,
                        help="If >0, set draft=true when quality total_score < value")
    parser.add_argument("--gate", action="store_true",
                        help="Run quality_gate after each render and write {slug}.quality.json")
    parser.add_argument("--schema", default="data/schema/article.schema.json",
                        help="Schema path used when --gate is set")
    parser.add_argument("--raw-amazon", default="data/raw/amazon.json",
                        help="Raw amazon.json used to back-fill badge fields (availability/loyalty_points/savings_percentage)")
    parser.add_argument("--per-asin-root", default="data/raw/per_asin",
                        help="Directory holding per-ASIN amazon snapshots used as a back-fill fallback when raw/amazon.json no longer contains the ASIN")
    parser.add_argument("--hugo-config", default="hugo/config.toml",
                        help="Hugo config used to derive site base path for internal links on competitor cards")
    args = parser.parse_args()

    evaluate_article = None
    schema: dict[str, Any] = {}
    if args.gate:
        try:
            from quality_gate import evaluate_article  # type: ignore
        except ImportError as e:
            print(f"--gate requested but quality_gate import failed: {e}")
            return
        schema_path = pathlib.Path(args.schema)
        if schema_path.exists():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"--gate: schema parse error ({schema_path}): {e}")
                schema = {}

    src_path = pathlib.Path(args.src)
    dst_path = pathlib.Path(args.dst)
    dst_path.mkdir(parents=True, exist_ok=True)
    raw_amazon_index = _load_raw_amazon_index(pathlib.Path(args.raw_amazon))
    per_asin_root = pathlib.Path(args.per_asin_root)
    raw_root = pathlib.Path(args.raw_amazon).parent
    rakuten_matched_index = _load_matched_index(raw_root / "rakuten_matched.json")
    yahoo_matched_index = _load_matched_index(raw_root / "yahoo_matched.json")
    asin_to_slug = _build_asin_to_slug_map(src_path)
    site_base_path = _site_base_path(pathlib.Path(args.hugo_config))
    git_history = _load_git_history(src_path)

    template_file = pathlib.Path("scripts/templates/post.md.j2")
    if not template_file.exists():
        print("Template not found")
        return

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_file.parent)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_file.name)

    rendered = 0
    skipped_legacy = 0
    for f in sorted(src_path.glob("*.json")):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            slug = data.get("slug", f.stem)
            legacy_v3 = "narrative" not in data
            if legacy_v3:
                data.setdefault("narrative", {k: "" for k in (
                    "lead", "why_this_product", "gift_appeal",
                    "daily_use", "safety_note", "closing")})
                data.setdefault("persona_fit", {})
                data.setdefault("faq", [])
                data.setdefault("keywords", [])
                skipped_legacy += 1
            _merge(data, _load_optional_json(src_path / f"{slug}.enrichment.json"), ENRICHMENT_KEYS)
            _merge(data, _load_optional_json(src_path / f"{slug}.seo.json"), SEO_KEYS)
            _backfill_amazon_badges(data, raw_amazon_index, per_asin_root)
            _attach_market_prices(data, rakuten_matched_index, yahoo_matched_index)
            _backfill_product_images(data, raw_amazon_index, per_asin_root)
            _override_competitive_analysis(data, per_asin_root)
            _fallback_news_books(data, per_asin_root)
            _fallback_youtube_embeds(data, per_asin_root)
            _attach_omcha_related(data, per_asin_root)
            _attach_internal_links(data, asin_to_slug, site_base_path)
            _fill_jsonld(data)

            _meta_re = re.compile(r"\s*[(（]\s*\d+\s*字\s*[)）]\s*$")
            if isinstance(data.get("narrative"), dict):
                for k in ("lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing"):
                    v = data["narrative"].get(k)
                    if isinstance(v, str):
                        data["narrative"][k] = _meta_re.sub("", v).strip()
            for top_key in ("editorial_comment", "expert_take"):
                v = data.get(top_key)
                if isinstance(v, str):
                    data[top_key] = _meta_re.sub("", v).strip()

            # 2026-05-15 (@J Phase 2): テンプレが参照する product.ivs_detail を
            # 新スコアで上書きしてから render する (本文内 IVS 表示を frontmatter と同期)。
            _sync_ivs_for_render(data)
            md_body = template.render(**data)
            draft = _quality_draft(slug, src_path, args.min_score)
            post = frontmatter.Post(md_body, **_frontmatter_meta(data, slug, draft, git_history, f))

            out_file = dst_path / f"{slug}.md"
            out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
            tag = "  [DRAFT: low quality]" if draft else ""

            if args.gate and evaluate_article is not None:
                report = evaluate_article(
                    f, schema, out_file,
                    rakuten_idx=rakuten_matched_index,
                    yahoo_idx=yahoo_matched_index,
                )
                qpath = src_path / f"{slug}.quality.json"
                qpath.write_text(
                    json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if args.min_score > 0 and report.total_score < args.min_score and not draft:
                    post.metadata["draft"] = True
                    out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
                    tag = f"  [DRAFT: score {report.total_score} < {args.min_score}]"
                else:
                    tag += f"  [score {report.total_score}]"

            legacy_tag = "  [legacy v3 fallback]" if legacy_v3 else ""
            print(f"Rendered: {out_file}{tag}{legacy_tag}")
            rendered += 1
        except Exception as e:
            print(f"Error processing {f}: {e}")
    msg = f"\nDone. {rendered} post(s) rendered."
    if skipped_legacy:
        msg += f" {skipped_legacy} rendered as legacy v3 fallback (regenerate via Jules)."
    print(msg)


if __name__ == "__main__":
    main()
