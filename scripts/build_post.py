"""Build Hugo post markdown from article JSON + enrichment + seo (おもちゃいろ v4).

For each ``data/articles/{slug}.json`` (excluding ``.enrichment.json`` /
``.seo.json`` / ``.quality.json`` siblings), merge the optional STAGE-2 and
STAGE-3 outputs and render ``hugo/content/posts/{slug}.md``.

Frontmatter is populated from STAGE-3 SEO data when available (optimized title /
meta description / jsonld / breadcrumbs), falling back to STAGE-1 fields.

Usage:
    python scripts/build_post.py
    python scripts/build_post.py --src data/articles/ --dst hugo/content/posts/
    python scripts/build_post.py --gate --min-score 70   # mark below-threshold as draft

``--min-score`` now requires ``--gate``: the article is scored in-place at render
time. The old path that read a ``<slug>.quality.json`` sidecar was retired in
#4826 (the sidecar was .gitignore'd and never existed in CI, so that path was
dead there anyway).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import urllib.parse
from datetime import datetime
from typing import Any, Optional

import frontmatter
import jinja2

import market_prices
import price_overlay
import stock_status
import where_to_buy_format
from brand_normalizer import normalize as normalize_brand
from build_feature_lists import PRICE_BANDS
from score_calculator import (
    ScoreResult,
    calculate as calculate_score,
    compute_ivs_axes,
    get_media_exposure_metrics,
    reset_media_exposure_metrics,
)
from term_slug import TermSlugMap

# #2817 Phase 2: JP タグ/ブランド名を Hugo タクソノミー URL (tags/brands) に
# 書き込む直前で英語スラッグへ変換する。data/articles/*.json (原本) は JP のまま
# 触らない — 変換はこの Hugo content 生成の一点だけで起きる。
_TERM_SLUGS = TermSlugMap()


def _term_to_slug(term: str) -> str:
    """data/term_slugs.yaml から引く。未登録の新規用語は get_or_create() で
    その場生成する (brand alias 優先 → ASCII透過 → pykakasi ローマ字化の同じ
    品質ロジック)。ただし build_post.py はこの新規割当を data/term_slugs.yaml
    へ永続化しない (in-memory のみ) — 永続化は 04-validate-article-pr.yml の
    ensure_tag_slugs.py (#2817 Phase 4) が PR 内で責任を持つ。build 時の
    get_or_create() はそれが未実行の場合でも壊れた slug を出さない安全網。
    """
    return _TERM_SLUGS.get_or_create(term)


SUFFIX_SKIP = (".enrichment", ".seo", ".quality")

# Issue #2711: a rewrite produces a new ``YYYY-MM-DD-<ASIN>.json`` body alongside
# the old one ("replace, don't pre-delete"); the stale body is removed only after
# the replacement lands (rewrite_queue.cleanup_completed). During that window two
# bodies share the same ``/products/<ASIN>/`` URL, so render only the newest-dated
# body per ASIN to avoid a duplicate-URL collision. A not-yet-regenerated ASIN
# keeps rendering its old body (no 404). Sorting by stem puts the newest date last.
_PRIMARY_RE = re.compile(r"-(B0[A-Z0-9]{8})$")


def _winning_stems(src_path: pathlib.Path) -> set[str]:
    """Return the set of file stems that are the newest body per ASIN.

    Files whose stem does not match ``*-<ASIN>`` (no parseable ASIN) always win
    (kept as-is) so unrelated/legacy filenames are never dropped.
    """
    newest: dict[str, str] = {}
    passthrough: set[str] = set()
    for f in src_path.glob("*.json"):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        m = _PRIMARY_RE.search(f.stem)
        if not m:
            passthrough.add(f.stem)
            continue
        asin = m.group(1)
        # Stem is ``YYYY-MM-DD-<ASIN>``; lexical max == newest date.
        if asin not in newest or f.stem > newest[asin]:
            newest[asin] = f.stem
    return set(newest.values()) | passthrough


def _build_score_context(sr: ScoreResult) -> dict[str, Any]:
    """Build the ``score`` dict consumed by ``post.md.j2`` (hero + recap blocks).

    Pure: only reads ``sr`` and never mutates ``data`` / ``product``. Replaces
    the legacy ``_sync_ivs_for_render`` which mutated ``product.ivs_detail`` in
    place (#1111 / Phase 2/3 of #1107).

    Coordinate convention: viewBox 400x280, center (200,140), radius cap 90.
    Order = top (cost) / right (safety) / bottom (longevity) / left (education).
    """
    bd = sr.breakdown
    axes = compute_ivs_axes(bd)
    cx, cy, rmax = 200.0, 140.0, 90.0
    radar = [
        ("cost", axes["cost_performance"], cx, cy - rmax),     # 上
        ("safety", axes["safety"], cx + rmax, cy),             # 右
        ("longevity", axes["longevity"], cx, cy + rmax),       # 下
        ("education", axes["education"], cx - rmax, cy),       # 左
    ]
    points = []
    for _key, val, ex, ey in radar:
        ratio = max(0.0, min(1.0, float(val) / 5.0))
        px = round(cx + (ex - cx) * ratio, 1)
        py = round(cy + (ey - cy) * ratio, 1)
        points.append(f"{px},{py}")
    return {
        "total_100": sr.total_100,
        "education": axes["education"],
        "longevity": axes["longevity"],
        "safety": axes["safety"],
        "cost_performance": axes["cost_performance"],
        "radar_points": " ".join(points),
        "score_rationale": [
            {"factor": "ブランド信頼度", "delta": f"+{bd['brand_tier']}/25", "reason": sr.rationale[0]},
            {"factor": "安全認証", "delta": f"+{bd['safety_cert']}/10", "reason": sr.rationale[1]},
            {"factor": "対象年齢", "delta": f"+{bd['age_fit']}/10", "reason": sr.rationale[2]},
            {"factor": "知育価値", "delta": f"+{bd['edu_value']}/15", "reason": sr.rationale[3]},
            {"factor": "メディア露出", "delta": f"+{bd['media_exposure']}/15", "reason": sr.rationale[4]},
            {"factor": "正規流通", "delta": f"+{bd['multi_market']}/10", "reason": sr.rationale[5]},
            {"factor": "コスパ", "delta": f"+{bd['price_value']}/15", "reason": sr.rationale[6]},
        ],
    }

BADGE_FIELDS = ("availability", "loyalty_points", "savings_percentage", "free_shipping")


def amazon_stock_state(availability: Any) -> str:
    """Amazon PA-API の availability メッセージから在庫状態を分類する。

    返り値: "instock" / "low_stock" / "outofstock" / "preorder" / "unknown"
    楽天/Yahoo は fetch_cross_search.py で正規化済 (instock/outofstock) だが
    Amazon は自由記述文字列のためテンプレ側で分類する必要がある。
    """
    if not availability or not isinstance(availability, str):
        return "unknown"
    text = availability.strip()
    if not text:
        return "unknown"
    if "在庫切れ" in text or "入荷時期は未定" in text or "取扱い終了" in text:
        return "outofstock"
    if "発売予定日" in text or "予約受付" in text:
        return "preorder"
    if "残り" in text and "点" in text:
        return "low_stock"
    if "在庫あり" in text or text.startswith("通常"):
        return "instock"
    return "unknown"

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


# Issue #1301 B4 Stage 2: Article 単位 JSON-LD の reviewedBy / creator 用定数。
# Person @id / URL は hugo/config.toml の authorProfile.founders と
# hugo/content/author/<slug>.md の Person @id (#person fragment) と同期必須。
# 変更時は 3 箇所同時更新。
_SITE_BASE_URL = "https://navi.omcha.jp"

PERSON_IROPAPA: dict[str, Any] = {
    "@type": "Person",
    "@id": f"{_SITE_BASE_URL}/author/iropapa/#person",
    "name": "いろパパ",
    "url": f"{_SITE_BASE_URL}/author/iropapa/",
}
PERSON_IROMAMA: dict[str, Any] = {
    "@type": "Person",
    "@id": f"{_SITE_BASE_URL}/author/iromama/#person",
    "name": "いろママ",
    "url": f"{_SITE_BASE_URL}/author/iromama/",
}

# 安全性 reviewer (いろママ) 判定のシグナル。
_MAMA_NAME_TOKENS: tuple[str, ...] = (
    "ままごと", "おままごと", "ぬいぐるみ", "歯固め", "おしゃぶり", "離乳",
)
_MAMA_TAG_TOKENS: frozenset[str] = frozenset({
    "食育", "ベビー", "0歳", "1歳", "2歳", "ぬいぐるみ", "安全", "離乳",
})

# 分析 reviewer (いろパパ) 判定のシグナル。
_PAPA_NAME_TOKENS: tuple[str, ...] = (
    "プログラミング", "ロボット", "stem", "知育ロボ", "ドローン", "コーディング",
)
_PAPA_TAG_TOKENS: frozenset[str] = frozenset({
    "stem", "プログラミング", "ロボット", "コスパ", "電子工作", "実験",
})

# 3 歳未満は安全性 reviewer (誤飲リスク年齢圏)。
_AGE_MAMA_THRESHOLD_MONTHS: int = 36


def _determine_reviewers(
    product: dict[str, Any], tags: list[str] | None
) -> list[dict[str, Any]]:
    """商品特性に応じて記事の reviewer (Person) を判定する。

    Issue #1301 B4 Stage 2: AI 下書き + 人間レビューを JSON-LD で分離するための
    担当 founder ルーティング。

    判定優先順位:
        1. いろママ — 安全性が主軸: 36 ヶ月未満 / 安全性タグ / 食育系名
        2. いろパパ — 分析が主軸: STEM / プログラミング / コスパ タグ・名
        3. どちらでもない — 両者連名 (default = dual review で E-E-A-T 強)

    Returns:
        Person dict のリスト (要素数 1 or 2)。
    """
    try:
        age_min = int(product.get("age_min_months") or 0)
    except (TypeError, ValueError):
        age_min = 0

    name_lower = str(product.get("name") or "").lower()
    tags_lower = {str(t).lower() for t in (tags or []) if t}
    mama_tag_lower = {t.lower() for t in _MAMA_TAG_TOKENS}
    papa_tag_lower = {t.lower() for t in _PAPA_TAG_TOKENS}

    # いろママ判定 (誤飲リスク年齢 + 食育・安全タグ + 食/縫いぐるみ系名)
    if (
        (age_min and age_min < _AGE_MAMA_THRESHOLD_MONTHS)
        or (tags_lower & mama_tag_lower)
        or any(tok.lower() in name_lower for tok in _MAMA_NAME_TOKENS)
    ):
        return [PERSON_IROMAMA]

    # いろパパ判定 (STEM・分析タグ + 機械系名)
    if (
        (tags_lower & papa_tag_lower)
        or any(tok.lower() in name_lower for tok in _PAPA_NAME_TOKENS)
    ):
        return [PERSON_IROPAPA]

    # default: 両者連名 (dual review)
    return [PERSON_IROPAPA, PERSON_IROMAMA]


# AI 下書き責任の Organization (creator) 定数。
ORG_OMOCHAIRO_EDITORIAL: dict[str, Any] = {
    "@type": "Organization",
    "name": "おもちゃいろ編集部",
    "url": f"{_SITE_BASE_URL}/about/",
}
ORG_AI_AGENT: dict[str, Any] = {
    "@type": "Organization",
    "name": "おもちゃロボ (AI 編集システム)",
    "description": "Amazon・楽天・Yahoo! ショッピングの市場データを集約し、記事下書きとスコア計算を担当する AI エージェント。",
}


def _build_webpage_jsonld(
    *,
    product: dict[str, Any],
    tags: list[str] | None,
    title: str,
    date: str,
    lastmod: str,
    asin: str,
) -> dict[str, Any]:
    """商品ページの WebPage JSON-LD を構築する。

    Issue #1301 B4 Stage 2:
        - author     = Organization (公開責任 = おもちゃいろ編集部)
        - reviewedBy = Person (担当 founder、_determine_reviewers で判定)
        - creator    = Organization (AI 編集システム / 下書き責任)

    既存の Product / FAQ / Breadcrumb JSON-LD と並列で emit される (既存非破壊)。
    """
    reviewers = _determine_reviewers(product, tags)
    page_url = f"{_SITE_BASE_URL}/products/{asin.lower()}/"
    reviewed_by: Any = reviewers[0] if len(reviewers) == 1 else reviewers
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": f"{page_url}#webpage",
        "url": page_url,
        "name": title,
        "author": ORG_OMOCHAIRO_EDITORIAL,
        "reviewedBy": reviewed_by,
        "creator": ORG_AI_AGENT,
        "datePublished": date,
        "dateModified": lastmod,
        "isPartOf": {
            "@type": "WebSite",
            "url": f"{_SITE_BASE_URL}/",
        },
    }


# AI 編集システム (おもちゃロボ) を Review の author として利用する Person 定義。
# Person 表現の理由: schema.org の Review.author は Person/Organization。
# Google の Review snippet 適格条件で「author は識別可能な実体」が求められる。
# Organization (ORG_AI_AGENT) ではなく Person 扱いで sameAs と organization 親で
# 「AI 編集システムである」事実を示しつつ、Review snippet として認識される形にする。
PERSON_OMOCHAIRO_BOT: dict[str, Any] = {
    "@type": "Person",
    "@id": f"{_SITE_BASE_URL}/about/#omochairo-bot",
    "name": "おもちゃロボ",
    "url": f"{_SITE_BASE_URL}/about/",
    "description": (
        "Amazon・楽天・Yahoo! ショッピングの市場データを集約し、"
        "記事下書きとスコア計算を担当する AI 編集システム。"
        "本サイトの全レビューは おもちゃロボ の独自スコア (知育価値 / 長寿命性 / 安全性 / コスパ) に基づく。"
    ),
}


def _build_review_jsonld(
    *,
    product: dict[str, Any],
    title: str,
    date: str,
    avg_rating: float | int | str,
    review_body: str,
    asin: str,
    aggregate_rating: dict[str, Any] | None = None,
    offers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Issue #1301 B5: Product.aggregateRating とは別に独立した Review JSON-LD を構築。

    aggregateRating は「サイトの集約評価」を示すが、Google の Review snippet 適格には
    「個別レビュー (Review エンティティ)」も必要。本関数は おもちゃロボ による
    独自レビュー 1 件を Review として emit する。

    - author: PERSON_OMOCHAIRO_BOT (AI 編集システム)
    - itemReviewed: Product (name/image を最小限再掲)
    - reviewRating: 1-5 スケール
    - reviewBody: editorial_comment / verdict / closing から抽出
    - datePublished: 記事公開日

    review_body が空 (= 抽出失敗) の場合は None を返し、emit しない。
    """
    name = product.get("name") or title
    image = product.get("image") or ""
    if not name or not review_body:
        return None
    # rating を 1-5 スケールに正規化
    try:
        rv = float(avg_rating)
    except (TypeError, ValueError):
        rv = 4.0
    if rv < 1.0:
        rv = 1.0
    elif rv > 5.0:
        rv = 5.0
    page_url = f"{_SITE_BASE_URL}/products/{asin.lower()}/"
    item_reviewed: dict[str, Any] = {
        "@type": "Product",
        "name": name,
    }
    if image:
        item_reviewed["image"] = image
    raw_brand = product.get("brand")
    if raw_brand:
        item_reviewed["brand"] = {"@type": "Brand", "name": raw_brand}
    # GSC 商品スニペット要件: itemReviewed の Product 自体も
    # offers / review / aggregateRating のいずれかを持たないと
    # 「裸の Product」として無効判定される。メイン Product と同じ
    # aggregateRating / offers を再掲し、独立した有効エンティティにする。
    if isinstance(aggregate_rating, dict) and aggregate_rating:
        item_reviewed["aggregateRating"] = aggregate_rating
    if isinstance(offers, dict) and offers:
        item_reviewed["offers"] = offers
    return {
        "@context": "https://schema.org",
        "@type": "Review",
        "@id": f"{page_url}#review",
        "itemReviewed": item_reviewed,
        "author": PERSON_OMOCHAIRO_BOT,
        "reviewRating": {
            "@type": "Rating",
            "ratingValue": f"{rv:.1f}",
            "bestRating": "5",
            "worstRating": "1",
        },
        "reviewBody": review_body,
        "datePublished": date,
        "publisher": ORG_OMOCHAIRO_EDITORIAL,
    }


def _build_howto_jsonld(
    *,
    product: dict[str, Any],
    data: dict[str, Any],
    asin: str,
) -> dict[str, Any] | None:
    """Issue #1301 B6: 「{product} を選ぶ前の確認ステップ」HowTo schema を構築。

    既存の narrative 構造 (lead / why_this_product / gift_appeal / daily_use /
    safety_note / closing) から 4-5 ステップを programmatic に組み立てる。

    HowTo schema は「複数ステップの手順」を要求するため、ナレーション本文を
    そのまま流すのではなく、確認すべき観点を 4 ステップに固定化する:

        Step 1: 対象年齢を確認 — age_min_months から月齢 / 歳を文字列化
        Step 2: 何が伸びるかを確認 — narrative.why_this_product から抜粋
        Step 3: 日常での使い方 — narrative.daily_use から抜粋
        Step 4: 安全認証を確認 — narrative.safety_note から抜粋

    各ステップ本文が空なら HowTo を emit しない (None 返し)。
    """
    name = product.get("name") or data.get("title") or ""
    if not name:
        return None
    nar = data.get("narrative") or {}
    if not isinstance(nar, dict):
        return None

    def _trim(text: Any, limit: int = 200) -> str:
        if not isinstance(text, str):
            return ""
        s = re.sub(r"\s+", " ", text.strip())
        if not s:
            return ""
        if len(s) <= limit:
            return s
        return s[: limit - 1].rstrip("、。・,. ") + "..."

    steps: list[dict[str, Any]] = []

    # Step 1: 対象年齢
    age_min: int = 0
    try:
        age_min = int(product.get("age_min_months") or 0)
    except (TypeError, ValueError):
        age_min = 0
    if age_min > 0:
        if age_min < 12:
            age_label = f"生後 {age_min} ヶ月以上"
        else:
            years = age_min // 12
            age_label = f"{years} 歳以上"
        steps.append({
            "@type": "HowToStep",
            "position": 1,
            "name": "対象年齢を確認",
            "text": f"{name}の対象年齢は {age_label}。お子さまの月齢/年齢が合うかを最初に確認してください。",
        })

    # Step 2: 何が伸びるか (why_this_product)
    why_text = _trim(nar.get("why_this_product"))
    if why_text:
        steps.append({
            "@type": "HowToStep",
            "position": len(steps) + 1,
            "name": "何が伸びるかを確認",
            "text": why_text,
        })

    # Step 3: 日常での使い方
    daily_text = _trim(nar.get("daily_use"))
    if daily_text:
        steps.append({
            "@type": "HowToStep",
            "position": len(steps) + 1,
            "name": "日常での使い方をイメージ",
            "text": daily_text,
        })

    # Step 4: 安全認証 / 安全性
    safety_text = _trim(nar.get("safety_note"))
    if safety_text:
        steps.append({
            "@type": "HowToStep",
            "position": len(steps) + 1,
            "name": "安全性を確認",
            "text": safety_text,
        })

    # Step 5 (optional): 贈り物としての適性
    gift_text = _trim(nar.get("gift_appeal"))
    if gift_text:
        steps.append({
            "@type": "HowToStep",
            "position": len(steps) + 1,
            "name": "贈り物としての適性を確認",
            "text": gift_text,
        })

    # 最低 2 ステップ無いと HowTo として弱い
    if len(steps) < 2:
        return None

    page_url = f"{_SITE_BASE_URL}/products/{asin.lower()}/"
    image = product.get("image") or ""
    howto: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "@id": f"{page_url}#howto",
        "name": f"{name} を選ぶ前に確認したい {len(steps)} つのポイント",
        "description": (
            f"{name} を購入する前にチェックすべき観点を、おもちゃロボが整理しました。"
            "対象年齢・知育効果・日常での使い方・安全性を順番に確認することで、"
            "ご家庭にぴったりかを判断できます。"
        ),
        "step": steps,
    }
    if image:
        howto["image"] = image
    return howto


def _extract_review_body(data: dict[str, Any]) -> str:
    """Issue #1301 B5: Review.reviewBody に流す本文を記事 JSON から抽出する。

    優先順位:
        1. editorial_comment (おもちゃロボの締め所感) — 簡潔・主観あり・最適
        2. narrative.closing — narrative の締め
        3. verdict — Stage 2 enrichment 後の結論
        4. narrative.lead — fallback

    280 文字以内に丸める (Google の Review snippet は短文を好む)。
    """
    candidates = [
        data.get("editorial_comment"),
    ]
    nar = data.get("narrative")
    if isinstance(nar, dict):
        candidates.append(nar.get("closing"))
        candidates.append(nar.get("lead"))
    candidates.append(data.get("verdict"))

    for c in candidates:
        if isinstance(c, str) and c.strip():
            s = re.sub(r"\s+", " ", c.strip())
            if len(s) <= 280:
                return s
            return s[:277].rstrip("、。・,. ") + "..."
    return ""


def _get_development_stage(age_min_months: int, stages_data: dict[str, Any]) -> dict[str, Any] | None:
    """最小月齢から、合致する最適な発達段階のデータを取り出す。"""
    if not stages_data:
        return None
    stage_keys = []
    for k in stages_data.keys():
        if k.endswith('m'):
            try:
                stage_keys.append(int(k[:-1]))
            except ValueError:
                pass
    stage_keys.sort()
    
    selected_key = 0
    for sk in stage_keys:
        if age_min_months >= sk:
            selected_key = sk
        else:
            break
            
    return stages_data.get(f"{selected_key}m")


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


def _build_article_index(src_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Return {asin: {slug, ivs_score_100, ivs_score, name, image, amazon_price}}.

    Used so competitor cards can deep-link to an existing internal article
    (with the article's IVS score badge) and so we can auto-recommend
    score-similar internal articles as additional competitor cards (#508).

    Scores are recomputed via :func:`score_calculator.calculate` for every
    article rather than read from ``product.ivs_detail.total_100``. The JSON
    field is written by Jules at article creation and only refreshed in-memory
    via the per-article ``data["score"]`` render context (built from a fresh
    ``ScoreResult`` in the main loop, post enrichment + price backfill), so
    cross-article references (similar / same-price-band cards) would otherwise
    surface stale Jules-era values that disagree with the score on the target
    article's own page (#1089).
    """
    index: dict[str, dict[str, Any]] = {}
    if not src_path.exists():
        return index
    winners = _winning_stems(src_path)  # #2711: newest body per ASIN only
    for f in src_path.glob("*.json"):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        if f.stem not in winners:
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = meta.get("slug") or f.stem
        product = meta.get("product") if isinstance(meta.get("product"), dict) else {}
        asin = product.get("asin")
        if not asin:
            m = re.search(r"-(B0[A-Z0-9]{8})$", f.stem)
            if m:
                asin = m.group(1)
        if not asin or asin in index:
            continue
        amazon_price_obj = (product.get("prices") or {}).get("amazon") or {}
        try:
            amazon_price = int(amazon_price_obj.get("price") or 0) if isinstance(amazon_price_obj, dict) else 0
        except (TypeError, ValueError):
            amazon_price = 0

        ivs_score_100: int | None = None
        ivs_score: float | None = None
        raw_brand = product.get("brand")
        if raw_brand:
            try:
                sr = calculate_score(meta, normalize_brand(raw_brand), asin=asin)
                ivs_score_100 = int(sr.total_100)
                ivs_score = float(sr.ivs_score)
            except Exception:
                # Fall through to JSON-cached value below if recompute fails.
                ivs_score_100 = None
                ivs_score = None
        if ivs_score_100 is None:
            ivs_detail = product.get("ivs_detail") if isinstance(product.get("ivs_detail"), dict) else {}
            cached_100 = ivs_detail.get("total_100")
            if isinstance(cached_100, (int, float)) and cached_100 > 0:
                ivs_score_100 = int(cached_100)
            cached_score = product.get("ivs_score")
            if isinstance(cached_score, (int, float)) and cached_score > 0:
                ivs_score = float(cached_score)

        index[asin] = {
            "slug": slug,
            "ivs_score_100": ivs_score_100,
            "ivs_score": ivs_score,
            "name": product.get("name") or "",
            "image": product.get("image") or "",
            "amazon_price": amazon_price,
        }
    return index


def _build_asin_to_slug_map(src_path: pathlib.Path) -> dict[str, str]:
    """Back-compat thin wrapper around :func:`_build_article_index`."""
    return {asin: meta["slug"] for asin, meta in _build_article_index(src_path).items()}


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
    article_index: dict[str, dict[str, Any]],
    site_base_path: str,
) -> None:
    """Mark each competitor entry with ``internal_url`` and the existing
    article's IVS score badge when we already publish an article for that
    ASIN (#508). The current article's own ASIN is excluded so a card never
    self-links.

    Accepts either the rich ``_build_article_index`` mapping or the legacy
    ``_build_asin_to_slug_map`` ``{asin: slug}`` mapping for back-compat;
    in the latter case only ``internal_url`` is attached.
    """
    ca = data.get("competitive_analysis")
    if not isinstance(ca, list) or not article_index:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    self_asin = product.get("asin") if product else None
    for c in ca:
        if not isinstance(c, dict):
            continue
        asin = c.get("asin")
        if not asin or asin == self_asin:
            continue
        entry = article_index.get(asin)
        if entry is None:
            continue
        if isinstance(entry, str):
            # legacy {asin: slug} shape
            c["internal_url"] = f"{site_base_path}/products/{asin.lower()}/"
            continue
        # URL structure moved from /posts/{slug}/ to /products/{asin}/
        # (See #511). Hugo aliases handle 301 redirects for the old paths.
        c["internal_url"] = f"{site_base_path}/products/{asin.lower()}/"
        score_100 = entry.get("ivs_score_100")
        if isinstance(score_100, (int, float)) and score_100 > 0:
            c["internal_ivs_score_100"] = int(score_100)
        score = entry.get("ivs_score")
        if isinstance(score, (int, float)) and score > 0:
            c["internal_ivs_score"] = float(score)


# Issue #3332 N3 消費側: SEO sidecar (`<slug>.seo.json`) が提案する
# internal_link_suggestions を本文 (narrative) へ決定的に注入する。
_INTERNAL_LINK_SECTIONS: frozenset[str] = frozenset({
    "how_to_choose", "why_this_product", "daily_use", "gift_appeal",
})
_INTERNAL_LINK_DENYLIST: frozenset[str] = frozenset({
    "この商品", "こちら", "詳しくは", "関連記事",
})
_INTERNAL_LINK_DYNAMIC_WORDS: tuple[str, ...] = (
    "価格", "最安", "在庫", "セール", "%オフ", "割引",
)
# 「円」「ポイント」は素の語で弾くと「楕円形のピース」(図形パズル/形合わせ商品の
# 形状説明) や「着目ポイント」(= 要点) まで落ちるので、金額 (数字/漢数詞 + 円) と
# ポイント還元の文脈でのみ弾く。generate_faq_seo.py の禁止規則と同じ意図
# (動的事実 = 時間で変化する事実だけを禁止する)。
_INTERNAL_LINK_PRICE_RE = re.compile(r"[\d０-９一二三四五六七八九十百千万数][\d,，]*\s*円")
_INTERNAL_LINK_POINT_RE = re.compile(
    r"ポイント(還元|付与|進呈|アップ|バック|倍)|ポイントが?(貯ま|付く|つく)|ポイ活"
)
_INTERNAL_LINK_ANCHOR_MIN = 4
_INTERNAL_LINK_ANCHOR_MAX = 30
_INTERNAL_LINK_MAX_PER_ARTICLE = 3
# anchor に含まれると `[anchor](url)` の markdown リンク構文自体を破壊する文字
# (#3332 レビュー指摘: 例 anchor「テスト[注釈]付き」→ 壊れたリンクを生成する)。
_INTERNAL_LINK_ANCHOR_FORBIDDEN_CHARS: tuple[str, ...] = (
    "[", "]", "(", ")", "\n", "\r", "`",
)
# 文末句読点で終わる anchor は一文まるごとのリンク化に見え不自然 (供給側
# generate_internal_links.py と同じ規則を消費側でも独立実装。#3332 検証規則13)
_INTERNAL_LINK_SENTENCE_FINAL_PUNCTUATION: tuple[str, ...] = ("。", "！", "？", ".", "!", "?")

_INTERNAL_LINK_MD_RE = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
_INTERNAL_LINK_TAG_RE = re.compile(r"<[^>\n]+>")


def _internal_link_overlaps_markup(text: str, start: int, end: int) -> bool:
    """anchor の出現区間 ``[start, end)`` が既存の markdown リンクまたは HTML
    タグの区間と重なっているかを判定する (#3332 検証規則6: 入れ子防止)。"""
    for pattern in (_INTERNAL_LINK_MD_RE, _INTERNAL_LINK_TAG_RE):
        for m in pattern.finditer(text):
            if start < m.end() and end > m.start():
                return True
    return False


def _inject_internal_links(
    data: dict[str, Any],
    article_index: dict[str, dict[str, Any]],
    site_base_path: str,
) -> tuple[int, int]:
    """SEO sidecar (``<slug>.seo.json``) 由来の ``internal_link_suggestions`` を
    narrative 本文へ決定的な文字列置換で注入する (#3332 N3 消費側)。

    LLM 由来の提案は一切信用せず、逐語一致・許可セクション・実在確認などの
    検証規則に一つでも落ちた提案は無条件 drop する (fail-closed)。1 件の drop
    が他の提案の処理を妨げないよう提案ごとに独立して判定し、例外は投げない。

    検証規則 (全て drop 判定):
        1. anchor_text が対象段落の完全な部分文字列であること (逐語一致)
        2. section が how_to_choose/why_this_product/daily_use/gift_appeal のみ
        3. paragraph_index は対象セクションの範囲内 (str セクションは 0 のみ)
        4. target_asin が article_index (公開記事 index) に実在すること
        5. target_asin が自記事 ASIN でないこと (自己リンク禁止)
        6. anchor の出現位置が既存の markdown リンク/HTML タグと重ならないこと
        7. anchor 長が 4〜30 字であること
        8. anchor が汎用語 denylist と完全一致しないこと
        9. anchor に動的事実 (価格・在庫等) の禁止語を含まないこと
        10. 1 記事 3 本まで / 1 段落 1 本 / 1 セクション 1 本 / 同一 target 重複禁止
            (超過分は list 順で後のものを drop)
        11. anchor が文末句読点 (。！？.!?) で終わらないこと (一文まるごとの
            リンク化を防ぐ)

    戻り値: ``(injected, dropped)`` の件数タプル (build manifest 集計用)。
    """
    suggestions = data.get("internal_link_suggestions")
    if not isinstance(suggestions, list) or not suggestions:
        return (0, 0)

    narrative = data.get("narrative")
    if not isinstance(narrative, dict):
        return (0, len(suggestions))

    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    self_asin = str(product.get("asin") or "").strip().upper()

    injected = 0
    dropped = 0
    used_sections: set[str] = set()
    used_paragraphs: set[tuple[str, int]] = set()
    used_targets: set[str] = set()

    for sugg in suggestions:
        if injected >= _INTERNAL_LINK_MAX_PER_ARTICLE:
            dropped += 1
            continue
        if not isinstance(sugg, dict):
            dropped += 1
            continue

        section = sugg.get("section")
        anchor = sugg.get("anchor_text")
        target_asin_raw = sugg.get("target_asin")
        paragraph_index_raw = sugg.get("paragraph_index")

        # 規則2・10 (1 セクション 1 本): allowlist 外 or 既に使用済みのセクションは drop
        if section not in _INTERNAL_LINK_SECTIONS or section in used_sections:
            dropped += 1
            continue
        if not isinstance(paragraph_index_raw, int):
            dropped += 1
            continue
        paragraph_index = paragraph_index_raw

        # 規則3: paragraph_index の範囲チェック (str セクションは 0 のみ有効)
        section_value = narrative.get(section)
        if isinstance(section_value, str):
            if paragraph_index != 0:
                dropped += 1
                continue
            paragraph_text = section_value
        elif isinstance(section_value, list):
            if paragraph_index < 0 or paragraph_index >= len(section_value):
                dropped += 1
                continue
            paragraph_text = section_value[paragraph_index]
            if not isinstance(paragraph_text, str):
                dropped += 1
                continue
        else:
            dropped += 1
            continue

        # 規則10 (1 段落 1 本)
        para_key = (section, paragraph_index)
        if para_key in used_paragraphs:
            dropped += 1
            continue

        # 規則7・8・9: anchor 自体の健全性チェック
        if not isinstance(anchor, str) or not anchor:
            dropped += 1
            continue
        if not (_INTERNAL_LINK_ANCHOR_MIN <= len(anchor) <= _INTERNAL_LINK_ANCHOR_MAX):
            dropped += 1
            continue
        # markdown リンク構文を破壊する文字 ([ ] ( ) 改行 バッククォート) を含む
        # anchor は壊れたリンクを生成するため drop (#3332 レビュー指摘)。
        if any(ch in anchor for ch in _INTERNAL_LINK_ANCHOR_FORBIDDEN_CHARS):
            dropped += 1
            continue
        # 規則11: 一文まるごとのリンク化防止
        if anchor.endswith(_INTERNAL_LINK_SENTENCE_FINAL_PUNCTUATION):
            dropped += 1
            continue
        if anchor in _INTERNAL_LINK_DENYLIST:
            dropped += 1
            continue
        if (any(w in anchor for w in _INTERNAL_LINK_DYNAMIC_WORDS)
                or _INTERNAL_LINK_PRICE_RE.search(anchor)
                or _INTERNAL_LINK_POINT_RE.search(anchor)):
            dropped += 1
            continue

        # 規則1: 逐語一致 (最初の1出現のみ対象)
        pos = paragraph_text.find(anchor)
        if pos < 0:
            dropped += 1
            continue
        # 規則6: 入れ子防止
        if _internal_link_overlaps_markup(paragraph_text, pos, pos + len(anchor)):
            dropped += 1
            continue

        # 規則4・5・10: リンク先の実在・自己リンク・同一 target 重複禁止
        if not isinstance(target_asin_raw, str) or not target_asin_raw.strip():
            dropped += 1
            continue
        target_asin = target_asin_raw.strip().upper()
        if not target_asin or target_asin == self_asin:
            dropped += 1
            continue
        if target_asin in used_targets:
            dropped += 1
            continue
        if target_asin not in article_index:
            dropped += 1
            continue

        link_md = f"[{anchor}]({site_base_path}/products/{target_asin.lower()}/)"
        new_text = paragraph_text[:pos] + link_md + paragraph_text[pos + len(anchor):]
        if isinstance(section_value, str):
            narrative[section] = new_text
        else:
            section_value[paragraph_index] = new_text

        used_sections.add(section)
        used_paragraphs.add(para_key)
        used_targets.add(target_asin)
        injected += 1

    return (injected, dropped)


_NEARBY_SCORE_WINDOW = 5
_NEARBY_MAX_RECOMMENDATIONS = 2


def _recommend_nearby_competitors(
    data: dict[str, Any],
    article_index: dict[str, dict[str, Any]],
    site_base_path: str,
    window: int = _NEARBY_SCORE_WINDOW,
    limit: int = _NEARBY_MAX_RECOMMENDATIONS,
) -> None:
    """Append up to ``limit`` competitor cards picked from existing internal
    articles whose IVS 100-point score is within ``±window`` of the current
    article (#508). Cards are tagged ``recommended_by_score=True`` so the
    template can label them as score-similar suggestions.

    Skips ASINs already present in ``competitive_analysis`` and the article's
    own ASIN. Sort key: score-proximity ascending, then ASIN for determinism.
    """
    if limit <= 0 or not article_index:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    self_asin = product.get("asin") or ""
    target_score = (product.get("ivs_detail") or {}).get("total_100")
    if not isinstance(target_score, (int, float)) or target_score <= 0:
        return
    target_score = int(target_score)
    target_price = 0
    amazon_price = (product.get("prices") or {}).get("amazon") or {}
    if isinstance(amazon_price, dict):
        try:
            target_price = int(amazon_price.get("price") or 0)
        except (TypeError, ValueError):
            target_price = 0

    ca = data.get("competitive_analysis")
    if not isinstance(ca, list):
        ca = []
        data["competitive_analysis"] = ca
    taken_asins = {self_asin}
    for c in ca:
        if isinstance(c, dict) and c.get("asin"):
            taken_asins.add(c["asin"])

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for asin, meta in article_index.items():
        if asin in taken_asins:
            continue
        score = meta.get("ivs_score_100")
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        delta = abs(int(score) - target_score)
        if delta > window:
            continue
        candidates.append((delta, asin, meta))

    candidates.sort(key=lambda t: (t[0], t[1]))

    for delta, asin, meta in candidates[:limit]:
        ca.append({
            "asin": asin,
            "name": _shrink_competitor_name(meta.get("name") or ""),
            "image": meta.get("image") or "",
            "url": f"https://www.amazon.co.jp/dp/{asin}/?tag=zefiransesu-22",
            "internal_url": f"{site_base_path}/products/{asin.lower()}/",
            "internal_ivs_score_100": int(meta.get("ivs_score_100") or 0),
            "internal_ivs_score": float(meta.get("ivs_score") or 0) if meta.get("ivs_score") else None,
            "price": meta.get("amazon_price") or 0,
            "price_comparison": _price_comparison_label(target_price, meta.get("amazon_price") or 0),
            "feature_comparison": [],
            "differentiators": [],
            "recommended_by_score": True,
            "score_delta": delta,
        })


_SAME_PRICE_BAND_LIMIT = 4


def _resolve_price_band(price: int) -> dict[str, Any] | None:
    """Return the PRICE_BANDS entry matching ``price``, or None when outside.

    The bands are defined in :mod:`build_feature_lists` so this stays in
    lockstep with the /cospa/ navigator (links anchor to those tabs).
    """
    if not isinstance(price, int) or price <= 0:
        return None
    for band in PRICE_BANDS:
        if band["price_min"] <= price <= band["price_max"]:
            return band
    return None


def _recommend_same_price_band(
    data: dict[str, Any],
    article_index: dict[str, dict[str, Any]],
    site_base_path: str,
    limit: int = _SAME_PRICE_BAND_LIMIT,
) -> None:
    """Attach ``data["same_price_band"]`` for #586 topic-cluster internal links.

    Picks up to ``limit`` other published articles whose Amazon price falls in
    the same /cospa/ price band as the current article. Excludes the article's
    own ASIN and anything already surfaced via ``competitive_analysis`` (incl.
    score-similar recommendations from #1018) so the section never repeats a
    card the reader just saw above. Ordering: IVS score desc, then ASIN —
    same band already controls for price-tier comparability.

    Skips silently if the current article has no Amazon price, no price band
    match, or no eligible same-band peers.
    """
    if limit <= 0 or not article_index:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    self_asin = product.get("asin") or ""
    amazon_price_obj = (product.get("prices") or {}).get("amazon") or {}
    try:
        target_price = int(amazon_price_obj.get("price") or 0) if isinstance(amazon_price_obj, dict) else 0
    except (TypeError, ValueError):
        target_price = 0
    band = _resolve_price_band(target_price)
    if band is None:
        return

    taken_asins = {self_asin}
    ca = data.get("competitive_analysis")
    if isinstance(ca, list):
        for c in ca:
            if isinstance(c, dict) and c.get("asin"):
                taken_asins.add(c["asin"])

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for asin, meta in article_index.items():
        if asin in taken_asins:
            continue
        peer_price = meta.get("amazon_price") or 0
        if not (band["price_min"] <= peer_price <= band["price_max"]):
            continue
        score = meta.get("ivs_score_100")
        if not isinstance(score, (int, float)) or score <= 0:
            continue
        candidates.append((int(score), asin, meta))

    if not candidates:
        return
    candidates.sort(key=lambda t: (-t[0], t[1]))

    cards: list[dict[str, Any]] = []
    for score, asin, meta in candidates[:limit]:
        cards.append({
            "asin": asin,
            "name": _shrink_competitor_name(meta.get("name") or ""),
            "image": meta.get("image") or "",
            "internal_url": f"{site_base_path}/products/{asin.lower()}/",
            "internal_ivs_score_100": int(score),
            "price": meta.get("amazon_price") or 0,
        })

    # NOTE: key は "cards" (not "items")。dict.items は Python の builtin method
    # で、Jinja2 の getattr → __getitem__ 解決順だと
    # ``same_price_band.items`` が bound method を返してしまい
    # ``|length`` で TypeError になる (#1078 直後に検知)。
    data["same_price_band"] = {
        "band_key": band["key"],
        "band_label": band["label"],
        "band_url": f"{site_base_path}/cospa/#cospa-band-pane-{band['key']}",
        "cards": cards,
    }


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


# Marketplace / commerce listings carry no independent editorial value — they
# echo the seller's own copy. Exclude them from the third-party highlights so the
# block stays "外部の客観情報" rather than re-quoted sales copy.
_HIGHLIGHT_HOST_DENY = (
    "amazon.", "rakuten.", "yahoo.", "ebay.", "aliexpress.",
    "mercari.", "paypay", "qoo10.", "wish.com",
)
# Hosts that tend to host genuine evaluation / discussion / institutional info.
_HIGHLIGHT_HOST_PREFER = (
    "reddit.com", "note.com", "ameblo.jp", "hatena", "mamari", "comolib",
    "kakaku.com", "blog", "news", "library", ".gov", ".edu", "go.jp", "ac.jp",
)
# Parts-list / spec-dump markers — these read like a manual, not a评価.
_HIGHLIGHT_JUNK_MARKERS = ("Item number", "Qty.", "Part #", "Quantity.", "Symbol Part")

# Contact-info detectors (#4150 / Refs #2995): third-party snippets sometimes
# *are* a seller's contact block (電話/FAX/Eメール) rather than editorial
# content. Cloudflare rewrites any literal email address it renders into
# /cdn-cgi/l/email-protection, which 404s on GitLab Pages (no Cloudflare
# Worker there) — so quoting one breaks the page *and* republishes a third
# party's phone/fax/email as if it were our own content. Detection keys off
# the *entity itself* (an actual email address or a hyphen-grouped Japanese
# phone/fax number) rather than label words like "FAX" or "お問い合わせ先":
# label-only matching risks false positives on unrelated prose that happens
# to mention "TEL" or "FAX" in passing, whereas the entity pattern is
# unambiguous and also catches label-less numbers. Partial-masking (redacting
# just the number) was considered and rejected — it breaks quote fidelity and
# a stripped contact block has zero reader value anyway, so dropping the
# whole snippet is simpler and safer than trying to repair it.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Japanese phone/fax numbers: three hyphen-separated digit groups starting
# with 0 (03-3744-0909 / 0120-123-456 / 090-1234-5678), or the parenthesised
# area-code form ((03)3744-0909). Matching on digit *count and grouping*
# rather than digit density means genuine review figures like "対象年齢
# 3-5歳" or "全長 30cm" never match — no leading 0, no three-group hyphen
# chain.
_PHONE_RE = re.compile(
    r"(?<!\d)0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)"
    r"|(?<!\d)\(0\d{1,4}\)\d{1,4}-?\d{3,4}(?!\d)"
)


def _has_japanese(text: str) -> bool:
    return any(
        "぀" <= c <= "ヿ" or "一" <= c <= "鿿"
        for c in (text or "")
    )


def _highlight_has_contact_info(text: str) -> bool:
    """True when the snippet contains an email address or a Japanese
    phone/fax number, i.e. it reads like a business's contact block rather
    than editorial content."""
    s = text or ""
    return bool(_EMAIL_RE.search(s) or _PHONE_RE.search(s))


def _highlight_snippet_ok(snippet: str) -> bool:
    """True when the snippet reads like prose a reader benefits from, not a
    digit-dense parts list, a contact block, or a stub. Used to keep the
    highlights honest."""
    s = (snippet or "").strip()
    if len(s) < 30:
        return False
    if any(m in s for m in _HIGHLIGHT_JUNK_MARKERS):
        return False
    if _highlight_has_contact_info(s):
        return False
    digits = sum(c.isdigit() for c in s)
    return digits / len(s) <= 0.25


def _highlight_score(src: dict[str, Any]) -> int:
    """Rank a third-party source for the highlights block. Higher is better.
    Japanese prose wins (reader-relevant), editorial/forum/institutional hosts
    win, and a comfortable snippet length wins."""
    host = (src.get("host") or "").lower()
    snippet = src.get("snippet") or ""
    score = 0
    if _has_japanese(snippet):
        score += 3
    if any(h in host for h in _HIGHLIGHT_HOST_PREFER):
        score += 2
    if 60 <= len(snippet) <= 300:
        score += 1
    return score


def _attach_source_highlights(
    data: dict[str, Any],
    per_asin_root: pathlib.Path,
    limit: int = 3,
) -> None:
    """Populate ``data["source_highlights"]`` from the Tavily snapshot at
    ``data/raw/per_asin/<ASIN>/third_party_sources.json``.

    This is the honest reframe of #1370: rather than passing off product-page
    copy as「購入者の声」(which the data does not actually contain — see #496:
    review fetching is no-op'd), we surface what we *do* have — attributed
    third-party snippets (forums, blogs, institutional pages) — clearly labelled
    as external information with the source host shown. Commerce listings and
    spec dumps are filtered out; one snippet per host keeps the block diverse.
    Renders only when ≥1 usable snippet survives; the template skips otherwise.
    Jules-authored ``source_highlights`` (future reservation) is preserved."""
    if data.get("source_highlights"):
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return
    path = per_asin_root / asin / "third_party_sources.json"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return
    candidates = [
        s for s in sources
        if isinstance(s, dict) and s.get("url") and s.get("title")
        and not any(d in (s.get("host") or "").lower() for d in _HIGHLIGHT_HOST_DENY)
        and _highlight_snippet_ok(s.get("snippet") or "")
    ]
    candidates.sort(key=_highlight_score, reverse=True)
    picked: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for s in candidates:
        host = (s.get("host") or "").lower()
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        picked.append({
            "title": s.get("title"),
            "url": s.get("url"),
            "snippet": (s.get("snippet") or "").strip(),
            "host": s.get("host") or "",
        })
        if len(picked) >= limit:
            break
    if picked:
        data["source_highlights"] = picked


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


def _attach_price_freshness(data: dict[str, Any], per_asin_root: pathlib.Path) -> None:
    """#3568 E5: 価格カードの近くに表示する「価格情報取得日」を
    ``data["price_checked_at"]`` (YYYY-MM-DD) として添付する。

    データ源は ``_apply_price_overlay`` が実際に採用した観測の時刻
    (``prices.amazon.price_observed_at``) **だけ**。取得できなければ何も設定しない
    (post.md.j2 は ``{% if price_checked_at %}`` で未設定時は非表示)。

    per_asin snapshot の ``fetched_at`` への fallback は 2026-08-09 に廃止した。
    表示日付を価格と別ソースにすると「新しい日付 + 凍結価格」という矛盾表示になる
    (#4007 のコメントが禁じていたのはまさにこれ)。しかも fallback が実際に発火するのは
    **overlay が観測を採用できなかったときだけ**で、それは「その取得で価格が得られ
    なかった」ことを意味する。つまりこの経路は構造上、常に「価格が取れなかった取得の
    日付を、記事作成時の凍結価格に貼る」動作しかしない。実データ 1900 件の replay で
    87 件が該当し、価格の古さは中央値 62 日・最大 84 日だった。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return

    observed = ((product.get("prices") or {}).get("amazon") or {}).get("price_observed_at")
    if isinstance(observed, str) and len(observed) >= 10:
        data["price_checked_at"] = observed[:10]


_PRICE_HISTORY_MIN_POINTS = 3
_PRICE_HISTORY_MIN_SPAN_DAYS = 14
_PRICE_HISTORY_MAX_POINTS_SHOWN = 12


def _load_price_history_points(price_history_root: pathlib.Path, asin: str) -> list[dict[str, Any]]:
    """``data/price_history/<ASIN>.jsonl`` (price_history.append_price_point が
    書く append-only ログ) から有効な観測点 (source="amazon", price>0) を
    ts 昇順で返す。ファイルが無い/壊れた行は無視してベストエフォートで読む。
    """
    p = price_history_root / f"{asin.upper()}.jsonl"
    if not p.exists():
        return []
    points: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("source") != "amazon":
                continue
            price = rec.get("price")
            ts = rec.get("ts")
            if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
                continue
            if not isinstance(ts, str) or not ts:
                continue
            points.append({"ts": ts, "price": price})
    except OSError:
        return []
    points.sort(key=lambda r: r["ts"])
    return points


def _merge_price_points(*lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """複数レーンの観測点を (ts, price) 一致で dedupe して ts 昇順で返す。

    ``build_price_dashboard.load_merged_history`` /
    ``where_to_buy_format._merge_price_points`` と同じ流儀。
    """
    seen: set[tuple[str, int]] = set()
    merged: list[dict[str, Any]] = []
    for lane in lanes:
        for pt in lane:
            key = (pt["ts"], pt["price"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(pt)
    merged.sort(key=lambda r: r["ts"])
    return merged


def _attach_price_history(data: dict[str, Any], price_history_root: pathlib.Path,
                          price_watch_root: Optional[pathlib.Path] = None) -> bool:
    """#2953 C案 (訂正版): 自前計測の価格履歴をゲート判定してテンプレコンテキスト
    ``data["price_history"]`` に添付する。

    2 レーン (週次 ``price_history_root`` / 日次 ``price_watch_root``) を
    マージして読む。``price_watch_root`` 省略時は週次のみ (後方互換)。
    ここが週次だけを読んでいた頃、``all_time_low`` は「/price/ ダッシュボードの
    判定と一致させる」と謳いながら、実際にはダッシュボード
    (``load_merged_history`` = マージ済み) と別の点列を見ていて一致していなかった。

    描画ゲート: 有効点 (source=amazon, price>0) が 3 点以上 かつ 最古〜最新の
    スパンが 14 日以上。蓄積開始 (2026-07-07) 直後はどの ASIN もスパン不足で
    ゲートを通らないのが正しい挙動 (安全なロールアウト)。ゲートを通らなければ
    ``data`` は一切変更しない (post.md.j2 は ``{% if price_history %}`` で
    未設定時は何も描画しない)。

    ``all_time_low`` = 直近計測値 (latest_price) が全履歴の最安値 (min_price)
    以下かどうか。/price/ ダッシュボードの判定 (build_price_dashboard.py
    ``evaluate_drop``: ``current_price <= min(全履歴)``) と一致させる。

    Returns:
        ゲートを通って添付したら True。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return False
    points = _merge_price_points(
        _load_price_history_points(price_history_root, asin),
        _load_price_history_points(price_watch_root, asin) if price_watch_root is not None else [],
    )
    if len(points) < _PRICE_HISTORY_MIN_POINTS:
        return False
    oldest_ts = points[0]["ts"]
    newest_ts = points[-1]["ts"]
    try:
        oldest_dt = datetime.fromisoformat(oldest_ts.replace("Z", "+00:00"))
        newest_dt = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    span_days = (newest_dt - oldest_dt).days
    if span_days < _PRICE_HISTORY_MIN_SPAN_DAYS:
        return False

    prices = [pt["price"] for pt in points]
    min_price = min(prices)
    max_price = max(prices)
    min_price_point = next(pt for pt in points if pt["price"] == min_price)
    latest_price = points[-1]["price"]
    shown = points[-_PRICE_HISTORY_MAX_POINTS_SHOWN:]

    data["price_history"] = {
        "points": [{"date": pt["ts"][:10], "price": pt["price"]} for pt in shown],
        "min_price": min_price,
        "min_price_date": min_price_point["ts"][:10],
        "max_price": max_price,
        "latest_price": latest_price,
        "span_days": span_days,
        "all_time_low": latest_price <= min_price,
    }
    return True


def _amazon_snapshot_status(per_asin_root: pathlib.Path, asin: str) -> str:
    """per-ASIN snapshot の status ("gone" / "") を返す。

    fetch_amazon.refresh_article_snapshots (2026-07-07) が GetItems 応答から
    連続欠落した ASIN に status="gone" を書く。それ以外 (キー無し含む) は ""。
    """
    p = per_asin_root / asin / "amazon.json"
    if not p.exists():
        return ""
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""
    return snap.get("status") or ""


_AMAZON_TAG_RE = re.compile(r"[?&]tag=([\w-]+)")


def _apply_amazon_discontinued(
    data: dict[str, Any],
    per_asin_root: pathlib.Path,
) -> bool:
    """snapshot status=="gone" の ASIN を「Amazon 取り扱い終了」としてビルド時に
    反映する。記事 JSON には焼き込まない (焼き込みは後から判定改修が入ったとき
    全記事 backfill が必要になる — §4.7 youtube/news の轍)。

      - prices.amazon: discontinued=True / price=0 / url="" (404 リンクを出さない)
        / search_url=Amazon 検索 URL (アフィタグは旧 URL から継承)
      - best_price / best_platform を残存プラットフォームで再計算 (全滅なら 0
        → テンプレの最安値表示が消える)
      - 楽天/Yahoo にも直リンクが無ければ data["purchase_unavailable"]=True
        → テンプレ冒頭に販売終了 notice を出す

    呼び出しは _attach_market_prices の後 (楽天/Yahoo の band 検証は焼き込み
    済みの旧 Amazon 価格を anchor に使うため、ゼロ化はその後で行う)。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return False
    asin = product.get("asin")
    if not asin or _amazon_snapshot_status(per_asin_root, asin) != "gone":
        return False
    prices = product.setdefault("prices", {})
    if not isinstance(prices, dict):
        return False
    amazon = prices.get("amazon") if isinstance(prices.get("amazon"), dict) else {}
    old_url = amazon.get("url") or ""
    m = _AMAZON_TAG_RE.search(old_url)
    tag = m.group(1) if m else "zefiransesu-22"  # post.md.j2 affiliate_url macro と同じ既定
    amazon["discontinued"] = True
    amazon["price"] = 0
    amazon["url"] = ""
    # 在庫/還元系バッジは現存しない出品の情報なので全て落とす
    amazon["availability"] = ""
    amazon["loyalty_points"] = 0
    amazon["savings_percentage"] = 0
    amazon["free_shipping"] = False
    name = product.get("name") or ""
    if name:
        amazon["search_url"] = (
            f"https://www.amazon.co.jp/s?k={urllib.parse.quote(name)}&tag={tag}")
    prices["amazon"] = amazon
    # Amazon が唯一の価格だったケースでは _recompute_best_price は candidates
    # 空で何もしないので、先に 0 に落としてから残存プラットフォームで再計算する。
    product["best_price"] = 0
    product["best_platform"] = ""
    _recompute_best_price(product)

    # 記事 JSON の product.amazon_review_url は使われないが、Jules が埋めて
    # いた場合に備えてビルド時コンテキストからは落とす (dp が 404 のため)。
    product.pop("amazon_review_url", None)
    # 参考ソース欄の自商品 dp リンクも 404 になるので除外 (件数表示は
    # sources|length なので自動で追従する)。
    sources = data.get("sources")
    if isinstance(sources, list):
        data["sources"] = [
            s for s in sources
            if not (isinstance(s, dict) and "amazon.co.jp" in (s.get("url") or "")
                    and asin in (s.get("url") or ""))]

    def _has_direct_link(entry: Any) -> bool:
        return (isinstance(entry, dict) and bool(entry.get("url"))
                and not entry.get("is_search"))

    if not (_has_direct_link(prices.get("rakuten"))
            or _has_direct_link(prices.get("yahoo"))):
        data["purchase_unavailable"] = True
    return True


# 読者向け本文に混じった生の ASIN コードを落とすための正規表現 (2026-08-09)。
# narrative.how_to_choose (#3203 v7 で追加された「選び分け」節) に
# 『ベーシックプラスセット』（B0BD3WZG32）のような Amazon 内部コードがそのまま出ており、
# 実測で公開 1913 ページ中 107 ページ・144 箇所が該当した。quality_gate の
# check_how_to_choose は「competitors.json に載っている ASIN か」を検査する封じ込め
# チェックで、**言及すること自体は許可**しているため、この表示は素通りしていた。
_INLINE_ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b")
_ASIN_WITH_LABEL_RE = re.compile(r"(?:ASIN\s*[:：]?\s*)?\bB0[A-Z0-9]{8}\b")
_PAREN_GROUP_RE = re.compile(r"[（(]([^（()）]*)[）)]")
_PAREN_FILLER_RE = re.compile(r"^[\s、,・]*(?:など|等)?[\s、,・]*$")


def _strip_inline_asin_codes(value: Any) -> Any:
    """本文テキストから生の ASIN コードを取り除く (str / list[str] 対応)。

    括弧ごと消してよいのは「括弧の中身が ASIN (と『など』等の助辞) だけ」のとき。
    括弧に商品名などの実質的な情報が入っている場合はコードだけ抜いて括弧は残す。
    文中に地の文として埋め込まれた裸のコード (「同じ 3 種セットの B0XXXXXXXX は…」)
    は、消すと日本語が壊れるのでここでは触らない (生成側で禁じるべき事象)。
    """
    if isinstance(value, list):
        return [_strip_inline_asin_codes(v) if isinstance(v, str) else v for v in value]
    if not isinstance(value, str) or "B0" not in value:
        return value

    def _clean_paren(m: re.Match[str]) -> str:
        inner = m.group(1)
        if not _INLINE_ASIN_RE.search(inner):
            return m.group(0)
        cleaned = _ASIN_WITH_LABEL_RE.sub("", inner)
        cleaned = re.sub(r"[:：]\s*(?=[\s、,・]|$)", "", cleaned)
        cleaned = re.sub(r"[ 　]{2,}", " ", cleaned).strip()
        if _PAREN_FILLER_RE.match(cleaned):
            return ""
        return f"（{cleaned}）"

    out = _PAREN_GROUP_RE.sub(_clean_paren, value)
    # 「迷宮ボール B0G4R9XVY6」のように閉じ鉤括弧の直前に置かれた形
    out = re.sub(r"[ 　]+\bB0[A-Z0-9]{8}\b(?=[」』])", "", out)
    # 括弧の外に出た「ASIN: B0XXXXXXXX」ラベル形
    out = re.sub(r"[ 　]*ASIN\s*[:：]\s*\bB0[A-Z0-9]{8}\b", "", out)
    return re.sub(r"[ 　]{2,}", " ", out).strip()


def _apply_amazon_unpriced(data: dict[str, Any], per_asin_root: pathlib.Path) -> bool:
    """最新 snapshot が価格を返さない ASIN の凍結価格を落とす (2026-08-09)。

    在庫切れ・一時的な取り扱い不可の出品は GetItems が価格を返さない。
    ``price_overlay`` は price>=1 を要求するので観測を採用せず、記事 JSON に凍結された
    **記事作成時の価格**がそのまま価格カードに残る。status="gone" ではないので
    ``_apply_amazon_discontinued`` にも掛からない。結果として「2ヶ月前の価格 +
    🔴在庫切れバッジ」が現在価格として出る (実データ 1900 件中 87 件、中央値 62 日)。

    価格が取れなかったのだから現在価格は不明である。0 に落として
    「取り扱い確認」表示に倒し、最安値の候補からも外す。availability と url は
    今回の snapshot 由来の事実なので残す (在庫切れバッジと「再入荷を確認」CTA)。

    ``_apply_amazon_discontinued`` と同じ理由で ``_attach_market_prices`` の **後**に
    呼ぶこと (楽天/Yahoo の band 検証が旧 Amazon 価格を anchor に使うため)。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return False
    asin = product.get("asin")
    if not asin:
        return False
    prices = product.get("prices")
    if not isinstance(prices, dict):
        return False
    amazon = prices.get("amazon")
    if not isinstance(amazon, dict):
        return False
    if amazon.get("discontinued"):
        return False  # 取り扱い終了は専用処理が済んでいる
    # overlay が観測を採用していれば price は最新。触らない。
    if amazon.get("price_observed_at"):
        return False
    try:
        frozen = int(amazon.get("price") or 0)
    except (TypeError, ValueError):
        frozen = 0
    if frozen <= 0:
        return False  # 既に「取り扱い確認」表示

    item = _load_per_asin_amazon(per_asin_root, asin)
    if item is None:
        return False  # snapshot 自体が無いなら現況を知らない。従来どおり据え置く
    try:
        observed_price = int(item.get("price") or 0)
    except (TypeError, ValueError):
        observed_price = 0
    if observed_price > 0:
        return False  # 価格は取れている (stale guard で弾かれただけ) ので据え置く

    amazon["price"] = 0
    product["best_price"] = 0
    product["best_platform"] = ""
    _recompute_best_price(product)
    return True


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


# #4007: 本文 (Jules 生成テキスト) の 99.4% が価格リテラルを含むため、カード価格を
# 日次化すると乖離が大きいものは本文と目視で矛盾する。この割合を超えたら注記を出す。
_PRICE_BODY_STALE_PCT = 0.20


def _apply_price_overlay(
    data: dict[str, Any],
    watch_index: dict[str, price_overlay.PriceObservation],
    per_asin_root: pathlib.Path,
) -> str | None:
    """``product.prices.amazon`` の価格・在庫・割引率を最新観測で上書きする (#4007)。

    記事 JSON の Amazon 価格は記事生成時のまま凍結しており (``_backfill_amazon_badges``
    は BADGE_FIELDS しか埋めず、しかも既存値が勝つ)、2026-07-26 の実測では比較可能
    1499 件のうち 45% が現在価格とズレていた。楽天/Yahoo は ``_attach_market_prices``
    が matched JSON (日次) で毎ビルド上書きしているため、Amazon だけが凍結して
    「最安」表示が 109 件で反転していた。

    price_watch (日次) → per_asin (週次) の優先順は price_overlay に一本化してある。
    本関数は ``_attach_market_prices`` (末尾で ``_recompute_best_price`` を呼ぶ) より
    **前**に呼ぶこと。それで最安バッジ・front matter・JSON-LD の AggregateOffer まで
    同じ観測で整合する。

    戻り値は採用した観測の source ("price_watch" / "per_asin")。観測が無ければ None
    (記事 JSON の値をそのまま使う = 従来挙動のまま fail-soft)。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return None
    asin = product.get("asin")
    if not asin:
        return None

    obs = price_overlay.resolve(
        asin, watch_index=watch_index, per_asin_root=per_asin_root
    )
    if obs is None:
        return None

    prices = product.setdefault("prices", {})
    amazon = prices.get("amazon")
    if not isinstance(amazon, dict):
        amazon = {}
        prices["amazon"] = amazon

    frozen_price = amazon.get("price")
    price_overlay.apply_to_amazon_entry(amazon, obs)

    # 本文中の価格リテラル (執筆時点の価格 = 上書き前の値) との乖離判定。
    try:
        old = int(frozen_price or 0)
    except (TypeError, ValueError):
        old = 0
    new = obs.price or 0
    if old > 0 and new > 0 and abs(new - old) / old >= _PRICE_BODY_STALE_PCT:
        data["price_body_stale"] = True

    return obs.source


def _normalize_price_entries(data: dict[str, Any]) -> None:
    """prices.<store> に price キーを必ず持たせる (#3030 / #3195)。

    テンプレートは StrictUndefined で product.prices.amazon.price を直接読むため、
    キーが欠けていると記事 1 本が丸ごとレンダリング失敗し、ページが生成されない。
    それでも article_index には ASIN が載るので competitor カードだけが残り、
    存在しない /products/<asin>/ への内部リンク 404 になる (site-audit r3 の実例)。

    Amazon 取り扱いの無い商品 (楽天/Yahoo のみ) では prices に amazon キーが無く、
    _backfill_amazon_badges が badge 用の空 dict を作るため price が欠ける。
    price=0 はテンプレート側が「取り扱い確認」表示として正しく処理できる。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product or not isinstance(product.get("prices"), dict):
        return
    for key in ("amazon", "rakuten", "yahoo"):
        entry = product["prices"].get(key)
        if isinstance(entry, dict):
            entry.setdefault("price", 0)


# #4007 follow-up 1: 楽天/Yahoo の quality gate 定数・判定ロジックは
# scripts/market_prices.py へ移設し、build_feature_lists.py / build_category_hubs.py
# とロジックを共有する (コピー drift #2723 の再発防止)。ここではモジュール属性名を
# 既存のまま維持するだけの薄いエイリアス。scripts/analyze_threshold_relaxation.py /
# scripts/fetch_cross_search.py が ``build_post._matched_passes_quality`` を、
# scripts/tests/test_market_quality_gate.py が ``build_post._MARKET_*`` を直接
# import/assert しているため、この属性名は変更しない。
_MARKET_PRICE_BAND_LOW = market_prices.PRICE_BAND_LOW
_MARKET_PRICE_BAND_HIGH = market_prices.PRICE_BAND_HIGH
_MARKET_PRICE_BAND_EXTREME = market_prices.PRICE_BAND_EXTREME
_MARKET_COVERAGE_RATIO = market_prices.COVERAGE_RATIO
_MARKET_GENERIC_TOKENS = market_prices.GENERIC_TOKENS
_normalize_for_match = market_prices._normalize_for_match
_is_model_number = market_prices._is_model_number
_descriptor_hits_title = market_prices._descriptor_hits_title
_load_matched_index = market_prices.load_matched_index


def _load_query_intent_map(path: pathlib.Path) -> dict[str, str]:
    """data/analytics/query_intent.json (A-7, #1980) から
    ``/products/<asin>/`` page path → 主要検索意図 ("commercial"/"informational") の
    dict を返す。navigational や検出外ページ、ファイル不在・空・parse 失敗は無視し
    空 dict を返す (呼び出し側は必ず現行レイアウトへ fallback する)。
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for row in payload.get("detected", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        page = row.get("page")
        intent = row.get("dominant_intent")
        if not page or intent not in ("commercial", "informational"):
            continue
        page_path = urllib.parse.urlparse(page).path.lower()
        if page_path:
            out[page_path] = intent
    return out


def _load_canonical_overrides(path: pathlib.Path) -> dict[str, str]:
    """data/analytics/canonical_overrides.json (A-3 query cannibalization 統合,
    epic #1356) から ``/products/<asin>/`` page path → 統合先 canonical URL の
    dict を返す。カニバリ Issue (#2370 等) で「弱い方の記事を主役 URL へ
    canonical 集約する」と編集判断した場合に、front matter の canonicalURL
    (build_post.py が毎回再生成しても失われないよう data-driven にしたもの) を
    決めるために使う。ファイル不在・空・parse 失敗は無視し空 dict を返す
    (呼び出し側は自己 canonical のまま = 現行挙動へ fallback する)。
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for row in payload.get("overrides", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        page = row.get("page")
        canonical = row.get("canonical")
        if not page or not canonical:
            continue
        page_path = urllib.parse.urlparse(page).path.lower()
        if page_path:
            out[page_path] = canonical
    return out


# Phase 2 quality gate 本体は market_prices.matched_passes_quality に移設済み
# (#4007 follow-up 1)。ここは属性名を維持するだけのエイリアス。
_matched_passes_quality = market_prices.matched_passes_quality


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
            existing["verified"] = existing.get("verified", False)
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


def _enforce_amazon_image(
    data: dict[str, Any],
    raw_index: dict[str, dict[str, Any]],
    per_asin_root: pathlib.Path,
) -> bool:
    """#2812: 検証済み amazon.json の画像で ``product.image`` を強制上書きする。

    Jules (AI) はプロンプトの自律補完指示に基づき、より高解像度な画像 URL
    (``_AC_SX679_.jpg`` 等) を Web 検索から再構築して ``product.image`` を
    上書きすることがある。その際 ``+`` を含む画像 ID のパースを誤り、実在しない
    壊れた URL (404) を生成する事故が確認された (例 B0G5DLSTS8)。

    対策: 唯一の真実源である ``data/raw/per_asin/<ASIN>/amazon.json`` の
    ``image`` (無ければ ``images[0]``) で ``product.image`` を常に上書きする。
    同時に Jules 由来の ``product.images`` ギャラリーも amazon.json の検証済み
    配列で置き換え、ハルシネーション URL を本番から排除する。

    amazon.json に画像が無い ASIN は Jules 由来値を温存する (上書きしない)。
    上書きが発生したら True を返す (manifest 統計用)。

    #3314: 併せて ``product.image_width`` / ``image_height`` も authoritative
    image と同じ src から通す (fetch_amazon.py が per_asin snapshot に書く
    実寸。無ければキー自体を付けない fail-soft — Amazon の ``_SL500_`` は
    長辺500px指定で正方形とは限らないため、実寸が無い状態で固定値を出すと
    CLS を悪化させる)。
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return False
    asin = product.get("asin")
    if not asin:
        return False

    authoritative_image = ""
    authoritative_gallery: list[str] = []
    authoritative_width: int | None = None
    authoritative_height: int | None = None
    for src in (raw_index.get(asin), _load_per_asin_amazon(per_asin_root, asin)):
        if not isinstance(src, dict):
            continue
        img = src.get("image")
        gallery = [u for u in (src.get("images") or []) if isinstance(u, str) and u]
        if not authoritative_image and isinstance(img, str) and img:
            authoritative_image = img
        if not authoritative_image and gallery:
            authoritative_image = gallery[0]
        if not authoritative_gallery and gallery:
            authoritative_gallery = gallery
        if (
            authoritative_width is None
            and isinstance(img, str)
            and img
            and img == authoritative_image
        ):
            w, h = src.get("image_width"), src.get("image_height")
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                authoritative_width, authoritative_height = w, h

    if not authoritative_image:
        return False

    overwritten = product.get("image") != authoritative_image
    product["image"] = authoritative_image
    if authoritative_width and authoritative_height:
        product["image_width"] = authoritative_width
        product["image_height"] = authoritative_height
    else:
        product.pop("image_width", None)
        product.pop("image_height", None)
    # ギャラリーも検証済み配列で置換 (主画像が先頭)。amazon.json に複数枚あれば
    # ハルシネーション混入の可能性がある Jules 由来 images を捨てる。
    if authoritative_gallery:
        ordered = [authoritative_image] + [
            u for u in authoritative_gallery if u != authoritative_image
        ]
        product["images"] = ordered
    return overwritten


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


def _clamp_lastmod_to_date(lastmod: str, date_str: str) -> str:
    """#2814: lastmod が公開日 (date) より過去なら date に同期して返す。

    Jules は未来日ハルシネーション防止のため date を JST 当日 10 時に予約公開風
    で設定するが、PR の auto-merge は UTC で走るため git commit 時刻 (=lastmod)
    が date より数時間〜前日に巻き戻る。すると JSON-LD で dateModified <
    datePublished、sitemap lastmod の逆転、post_meta の「最終更新: 前日」誤表示
    を招く。実時点として lastmod < date のとき lastmod を date に丸める。

    aware/naive の混在等でパースに失敗した場合は日付文字列 (YYYY-MM-DD) 比較に
    fallback する (テンプレ post_meta.html が実際に比較するのも日付部分のため)。
    どちらも判定不能なら lastmod を素通しする。
    """
    if not lastmod or not date_str:
        return lastmod
    try:
        lm = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if (lm.tzinfo is None) != (dt.tzinfo is None):
            raise TypeError("naive/aware mismatch")
        return date_str if lm < dt else lastmod
    except (ValueError, TypeError):
        # 日付部分 (先頭 10 文字 YYYY-MM-DD) のみで比較
        if lastmod[:10] and date_str[:10] and lastmod[:10] < date_str[:10]:
            return date_str
        return lastmod


def _resolve_page_asin(product: dict[str, Any] | None, slug: str) -> str:
    """product.asin があればそれを使い、無ければ slug 末尾の ASIN パターンから復元する。
    front matter の url (``/products/<asin>/``) 生成と #1980 query_intent 照合キーの
    両方で同じ解決ロジックを使うための共有ヘルパー。
    """
    asin = (product or {}).get("asin")
    if asin:
        return asin
    m = re.search(r"-(B0[A-Z0-9]{8})$", slug, flags=re.IGNORECASE)
    return m.group(1) if m else slug


def _frontmatter_meta(
    data: dict[str, Any],
    slug: str,
    draft: bool,
    git_history: dict[str, dict[str, Any]],
    json_file_path: pathlib.Path,
    score_result: ScoreResult | None = None,
    query_intent_map: dict[str, str] | None = None,
    canonical_overrides: dict[str, str] | None = None,
    stock_title_override: str | None = None,
) -> dict[str, Any]:
    # #2686 / #4964: 「どこで買える/在庫」型が適用された記事は決定的生成の
    # タイトルを最優先で使う (元 JSON の title / title_variants は書き換えず、
    # build 時にだけ差し替える)。ロールアウト日以降 かつ 在庫状態が確定した
    # 記事だけがここに入るので、既存記事の title は一切影響を受けない。
    if stock_title_override:
        title = stock_title_override
    else:
        title = data.get("title", "No Title")
        variants = data.get("title_variants") or []
        if variants and isinstance(variants[0], dict) and variants[0].get("title"):
            title = variants[0]["title"]
    description = data.get("meta_description_optimized") or data.get("meta_description", "")

    product = data.get("product") or {}
    asin = _resolve_page_asin(product, slug)

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

    # #2814: 公開日より過去の lastmod を date に同期 (UTC commit 時差の逆転是正)
    article_date = data.get("date", datetime.now().isoformat())
    lastmod = _clamp_lastmod_to_date(lastmod, article_date)

    meta: dict[str, Any] = {
        "title": title,
        "date": article_date,
        "lastmod": lastmod,
        "update_count": update_count,
        # #2817 Phase 2: JP タグ文字列を英語スラッグへ変換して書き込む。表示名 (JP) は
        # content/tags/<slug>/_index.md の title front matter が担う (Hugo .Title/.LinkTitle)。
        "tags": [_term_to_slug(t.rstrip(". ")) for t in data.get("tags", []) if t],
        "slug": slug,
        "url": f"/products/{asin.lower()}/",
        "aliases": [f"/posts/{slug.lower()}/"],
        "draft": draft,
        "description": description,
        "keywords": data.get("keywords", []),
    }
    # #1980: A-7 (GSC query intent 分類, data/analytics/query_intent.json) がこの
    # ページの主要検索意図を確定させている場合のみ CTA レイアウトを切替。未検出
    # ページ・ファイル不在時は front matter に一切書かない = 現行レイアウト不変。
    if query_intent_map:
        cta_intent = query_intent_map.get(meta["url"])
        if cta_intent:
            meta["cta_layout"] = cta_intent
    # #2370: A-3 query cannibalization 統合。編集判断で「弱い方の記事を主役
    # URL へ集約する」と決めたページのみ canonicalURL を付与し、head.html の
    # <link rel="canonical"> をそちらに向ける (post_canonical.html の可視
    # "originally published at" リンクは ShowCanonicalLink 未設定のため出ない)。
    if canonical_overrides:
        canonical_target = canonical_overrides.get(meta["url"])
        if canonical_target:
            meta["canonicalURL"] = canonical_target
    image_url = product.get("image") or ""
    if image_url:
        meta["product_image"] = image_url
        # Issue #1300 ②: og:image / twitter:image を商品画像で上書き。
        # PaperMod の opengraph.html / twitter_cards.html は .Params.images が
        # 設定されていれば _funcs/get-page-images 経由で最初の URL を採用する
        # (未設定だと site.Params.images の /og-image.jpg 汎用画像に fallback)。
        #
        # Issue #1494: Amazon CDN の元画像が 500x455 程度しかない商品が多く、
        # og:image:width=1200 を宣言しても X (summary_large_image) / Threads /
        # FB で preview が劣化または抑止される。build 時に専用 1200x630 画像を
        # 合成して /og/<asin>.jpg に出力 → og:image をそちらに差し替える。
        # 失敗時は従来挙動 (Amazon 画像 URL を直接 og:image) に fallback。
        og_image_path = None
        try:
            from build_og_image import build_og_image  # type: ignore

            out = build_og_image(
                asin=asin,
                image_url=image_url,
                title=title,
                out_dir=pathlib.Path("hugo/static/og"),
            )
            if out:
                og_image_path = f"/og/{asin.lower()}.jpg"
        except Exception as e:
            print(f"build_og_image failed for {asin}: {e}")
        # Issue #1500: PR #1302 (#1300) で og:image を per-page 化した際、`meta["images"]`
        # を 1 枚に固定したため、PaperMod の opengraph.html 既存仕様 (`.Params.images`
        # の最大 6 枚を og:image として range emit) が活かされていなかった。
        # _backfill_product_images() で build 時に組み立てた product["images"]
        # (primary + PA-API variants 最大 6 枚) を残りの og:image 候補として併出する。
        #
        # platform 仕様: X (twitter:image) / Threads は単一画像しか採用しないが、
        # Facebook は share dialog で選択肢化、LinkedIn は最初の 1 枚を採用。
        # link preview UI の carousel 化は organic post の standard 仕様には無いが、
        # 「実装側で 1 枚に絞る」boundary は外し、platform の挙動は live 観測する。
        # 先頭は build-time 合成 OG (1200x630 brand band 化) を維持して X の
        # summary_large_image の主画像はそのまま。
        images_list: list[str] = []
        if og_image_path:
            images_list.append(og_image_path)
        for u in (product.get("images") or [])[:6]:
            if isinstance(u, str) and u and u not in images_list:
                images_list.append(u)
        if not images_list and image_url:
            images_list.append(image_url)
        meta["images"] = images_list[:6]   # PaperMod opengraph.html の `first 6` と一致
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
        # #2817 Phase 2: brands タクソノミー (Hugo /brands/<slug>/ の URL) だけ英語
        # スラッグ化する。meta["brand"] (単数, 構造化データ/表示用) は JP canonical
        # のまま維持する。
        if not nb.exclude_from_taxonomy:
            meta["brands"] = [_term_to_slug(nb.canonical)]
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

    # #1124: Jules 側の ivs_score / ivs_detail 生成は廃止。スコアは
    # brand_tier(25) + safety(10) + age(10) + edu(15) + media(15) + market(10) + price(15)
    # を score_calculator で算出する一本化された source-of-truth に揃える。
    if raw_brand:
        # #1107: 同記事の ScoreResult が事前計算済 (main loop が enrichment 後に算出) なら
        # それを再利用し、calculate_score の重複呼出を避ける。
        sr = score_result if score_result is not None else calculate_score(
            data, nb, asin=product.get("asin")
        )
        meta["ivs_score"] = sr.ivs_score
        meta["ivs_score_100"] = sr.total_100
        meta["score_breakdown"] = sr.breakdown
        # 4 軸 (#589) — カード一覧で簡易バーを描くために frontmatter に持たせる。
        # index.json / Hugo partial がここを読む。スケール定義は score_calculator 一元管理。
        meta["ivs_axes"] = compute_ivs_axes(sr.breakdown)
    # Issue #1297: Hugo の .Params map はキーを小文字化するため、JSON-LD を
    # ネスト dict のまま渡すと aggregateRating → aggregaterating のように
    # schema.org キーが壊れる。各値を JSON 文字列としてシリアライズし、
    # template 側は | safeJS で素通しする。
    # `<` / `>` / `&` は HTML escape して </script> 衝突を防ぐ
    # (Go encoding/json のデフォルト挙動と同等)。
    meta_jsonld: dict[str, Any] = {}
    if data.get("jsonld"):
        for k, v in data["jsonld"].items():
            if isinstance(v, (dict, list)):
                s = json.dumps(v, ensure_ascii=False)
                s = s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                meta_jsonld[k] = s
            else:
                meta_jsonld[k] = v
    # Issue #1301 B4 Stage 2: WebPage JSON-LD (author / reviewedBy / creator)。
    # AI 下書きと人間レビューを分離可視化する。既存 product/faq/breadcrumb と並列 emit。
    if product and asin:
        webpage_ld = _build_webpage_jsonld(
            product=product,
            tags=meta.get("tags") or [],
            title=title,
            date=str(data.get("date") or lastmod),
            lastmod=lastmod,
            asin=str(asin),
        )
        s = json.dumps(webpage_ld, ensure_ascii=False)
        s = s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        meta_jsonld["webpage"] = s

        # Issue #1301 B5: 個別 Review JSON-LD。Product.aggregateRating とは別に
        # おもちゃロボの独自レビュー 1 件を Review として emit。
        review_body = _extract_review_body(data)
        if review_body:
            avg_for_review: Any = 4.0
            rs = data.get("review_summary")
            if isinstance(rs, dict) and rs.get("avg_rating") is not None:
                avg_for_review = rs["avg_rating"]
            elif product.get("ivs_score") is not None:
                avg_for_review = product["ivs_score"]
            # _fill_jsonld が既に Product schema に aggregateRating/offers を
            # 注入済み。itemReviewed の Product にも同じものを再掲して
            # GSC「offers/review/aggregateRating が必要」エラーを防ぐ。
            _prod_ld = (data.get("jsonld") or {}).get("product") or {}
            review_ld = _build_review_jsonld(
                product=product,
                title=title,
                date=str(data.get("date") or lastmod),
                avg_rating=avg_for_review,
                review_body=review_body,
                asin=str(asin),
                aggregate_rating=_prod_ld.get("aggregateRating")
                if isinstance(_prod_ld, dict)
                else None,
                offers=_prod_ld.get("offers") if isinstance(_prod_ld, dict) else None,
            )
            if review_ld:
                s = json.dumps(review_ld, ensure_ascii=False)
                s = s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                meta_jsonld["review"] = s

        # Issue #1301 B6: HowTo JSON-LD (「選び方」確認ステップ)。
        # narrative の why_this_product / daily_use / safety_note / gift_appeal を
        # 4-5 ステップに programmatic に組み立てる。
        howto_ld = _build_howto_jsonld(product=product, data=data, asin=str(asin))
        if howto_ld:
            s = json.dumps(howto_ld, ensure_ascii=False)
            s = s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            meta_jsonld["howto"] = s

    if meta_jsonld:
        meta["jsonld"] = meta_jsonld
    if data.get("breadcrumbs"):
        meta["breadcrumbs"] = data["breadcrumbs"]
    # #3568 E5: 価格情報取得日 (_attach_price_freshness が設定済みの場合のみ)
    if data.get("price_checked_at"):
        meta["price_checked_at"] = data["price_checked_at"]

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--dst", default="hugo/content/posts/")
    parser.add_argument("--min-score", type=int, default=0,
                        help="If >0, set draft=true when quality total_score < value (requires --gate)")
    parser.add_argument("--gate", action="store_true",
                        help="Run quality_gate after each render (scores the article in-place; "
                             "no sidecar is written)")
    parser.add_argument("--schema", default="data/schema/article.schema.json",
                        help="Schema path used when --gate is set")
    parser.add_argument("--raw-amazon", default="data/raw/amazon.json",
                        help="Raw amazon.json used to back-fill badge fields (availability/loyalty_points/savings_percentage)")
    parser.add_argument("--per-asin-root", default="data/raw/per_asin",
                        help="Directory holding per-ASIN amazon snapshots used as a back-fill fallback when raw/amazon.json no longer contains the ASIN")
    parser.add_argument("--hugo-config", default="hugo/config.toml",
                        help="Hugo config used to derive site base path for internal links on competitor cards")
    parser.add_argument("--query-intent", default="data/analytics/query_intent.json",
                        help="A-7 query intent classification (#1980); used to set cta_layout front matter")
    parser.add_argument("--canonical-overrides", default="data/analytics/canonical_overrides.json",
                        help="A-3 query cannibalization canonical consolidation (#2370); used to set canonicalURL front matter")
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
    # #2953 C案: fetch_amazon.write_per_asin_snapshot が out_root.parent/price_history
    # に append する自前価格履歴。out_root は raw_root (通常 data/raw) と同じ基準。
    price_history_root = raw_root.parent / "price_history"
    # 日次レーン (price_watch)。週次レーン (price_history) とは書き込み元が別
    # リポジトリ (single-writer) だが、読む側は両方をマージする。2 レーンは
    # dedupe が独立に効いて位相がずれるため実測でほぼ相補で、片方を捨てると
    # 観測点を落とす。詳細は where_to_buy_format.build_price_history_note。
    price_watch_history_root = raw_root.parent / "price_watch" / "history"
    rakuten_matched_index = _load_matched_index(raw_root / "rakuten_matched.json")
    yahoo_matched_index = _load_matched_index(raw_root / "yahoo_matched.json")
    article_index = _build_article_index(src_path)
    site_base_path = _site_base_path(pathlib.Path(args.hugo_config))
    git_history = _load_git_history(src_path)
    query_intent_map = _load_query_intent_map(pathlib.Path(args.query_intent))
    if query_intent_map:
        print(f"cta_layout: loaded {len(query_intent_map)} page(s) with classified query intent")
    canonical_overrides = _load_canonical_overrides(pathlib.Path(args.canonical_overrides))
    if canonical_overrides:
        print(f"canonicalURL: loaded {len(canonical_overrides)} page(s) with A-3 canonical override")

    # 年齢別発達段階目安データのロード
    stages_path = pathlib.Path("hugo/data/development_stages.json")
    stages_data = {}
    if stages_path.exists():
        try:
            stages_data = json.loads(stages_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Failed to load development_stages.json: {e}")

    template_file = pathlib.Path("scripts/templates/post.md.j2")
    if not template_file.exists():
        print("Template not found")
        return

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_file.parent)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["amazon_stock_state"] = amazon_stock_state
    template = env.get_template(template_file.name)

    # #4007: 「現在の Amazon 価格」の日次観測 index。latest.json を 1 回読んで
    # 全記事で使い回す (無い/古い場合は per_asin 週次レーンへ自動フォールバック)。
    price_watch_index = price_overlay.load_watch_index()
    print(f"price_overlay: price_watch index = {len(price_watch_index)} ASIN")
    price_overlay_stats = {"price_watch": 0, "per_asin": 0, "none": 0, "body_stale": 0,
                           "unpriced_cleared": 0}

    # #2686 / #4964: 「どこで買える/在庫」記事型。stock_index は price_watch の
    # latest.json を 1 回読んで全記事で使い回す (price_watch_index と別読み
    # だが同じ小さいファイルなのでコスト無視できる — stock_status.py 側の
    # 設計判断: avail 文言は price_overlay の価格 index に無いので別途読む)。
    stock_index = stock_status.load_stock_index()
    stock_title_applied = 0

    rendered = 0
    skipped_legacy = 0
    discontinued_count = 0
    cta_layout_applied = 0
    # #3332 N3 消費側: internal_link_suggestions の注入/drop 件数を run 全体で合算。
    internal_links_injected_total = 0
    internal_links_dropped_total = 0

    # 充足率統計データの初期化
    stats = {
        "youtube": {"primary": 0, "fallback": 0, "missing": 0},
        "news": {"primary": 0, "fallback": 0, "missing": 0},
        "books": {"primary": 0, "fallback": 0, "missing": 0},
        "omcha": {"primary": 0, "fallback": 0, "missing": 0},
    }
    # #677: per-ASIN fallback breakdown. ``manifest["by_asin"][asin]["fallbacks"]``
    # records which path each media source took (primary / fallback_used / missing)
    # for drill-down beyond the aggregate summary.
    by_asin: dict[str, dict[str, Any]] = {}
    # score_calculator の missing/empty counter は build_post 経由の calculate_score
    # 呼出で蓄積されるため、明示的にリセットしてから loop を回す。
    reset_media_exposure_metrics()

    render_winners = _winning_stems(src_path)  # #2711: newest body per ASIN only
    for f in sorted(src_path.glob("*.json")):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        if f.stem not in render_winners:
            print(f"  SKIP (superseded by newer body, #2711): {f.name}")
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
            # フォールバック実行前の Jules 由来データの存在有無を記録
            has_jules_yt = bool(data.get("youtube_embeds"))
            has_jules_news = bool(data.get("news"))
            has_jules_books = bool(data.get("books"))
            has_jules_omcha = bool(data.get("omcha_related"))

            _merge(data, _load_optional_json(src_path / f"{slug}.enrichment.json"), ENRICHMENT_KEYS)
            _merge(data, _load_optional_json(src_path / f"{slug}.seo.json"), SEO_KEYS)
            _backfill_amazon_badges(data, raw_amazon_index, per_asin_root)
            # #4007: 凍結した Amazon 価格を日次観測で上書き (_attach_market_prices の
            # _recompute_best_price より前に置くことで最安バッジまで整合する)。
            overlay_src = _apply_price_overlay(data, price_watch_index, per_asin_root)
            if overlay_src:
                price_overlay_stats[overlay_src] += 1
            else:
                price_overlay_stats["none"] += 1
            if data.get("price_body_stale"):
                price_overlay_stats["body_stale"] += 1
            _attach_price_freshness(data, per_asin_root)
            _attach_market_prices(data, rakuten_matched_index, yahoo_matched_index)
            amazon_discontinued = _apply_amazon_discontinued(data, per_asin_root)
            # 最新 snapshot が価格を返さない出品の凍結価格を落とす (2026-08-09)。
            # _attach_market_prices の後・_apply_amazon_discontinued と同じ位置。
            if _apply_amazon_unpriced(data, per_asin_root):
                price_overlay_stats["unpriced_cleared"] += 1
            _attach_price_history(data, price_history_root, price_watch_history_root)
            # #2812: Jules の画像ハルシネーション (404) を防ぐため、検証済み
            # amazon.json の画像で product.image を強制上書きしてから gallery を補完。
            image_overwritten = _enforce_amazon_image(data, raw_amazon_index, per_asin_root)
            _backfill_product_images(data, raw_amazon_index, per_asin_root)
            _override_competitive_analysis(data, per_asin_root)
            _fallback_news_books(data, per_asin_root)
            _fallback_youtube_embeds(data, per_asin_root)
            _attach_omcha_related(data, per_asin_root)
            _attach_source_highlights(data, per_asin_root)
            _attach_internal_links(data, article_index, site_base_path)
            _recommend_nearby_competitors(data, article_index, site_base_path)
            _recommend_same_price_band(data, article_index, site_base_path)
            _fill_jsonld(data)
            # 価格を触る処理 (badges / market / discontinued / history) が
            # 出揃った後に、テンプレートが読む price キーの欠損を埋める。
            _normalize_price_entries(data)

            # 統計データの集計
            _po = data.get("product")
            asin_for_manifest = _po.get("asin") if isinstance(_po, dict) else ""
            per_article_fallbacks: dict[str, str] = {}

            def _update_stats(key: str, has_jules: bool, has_final: bool) -> None:
                if has_jules:
                    stats[key]["primary"] += 1
                    per_article_fallbacks[key] = "primary"
                elif has_final:
                    stats[key]["fallback"] += 1
                    per_article_fallbacks[key] = "fallback_used"
                else:
                    stats[key]["missing"] += 1
                    per_article_fallbacks[key] = "missing"

            _update_stats("youtube", has_jules_yt, bool(data.get("youtube_embeds")))
            _update_stats("news", has_jules_news, bool(data.get("news")))
            _update_stats("books", has_jules_books, bool(data.get("books")))
            _update_stats("omcha", has_jules_omcha, bool(data.get("omcha_related")))
            # #2812: amazon.json 検証済み画像で上書きが発生したら記録 (観察用)。
            if image_overwritten:
                per_article_fallbacks["image"] = "amazon_enforced"
            # Amazon 消滅 ASIN (snapshot status=gone) の取り扱い終了表示 (2026-07-07)。
            if amazon_discontinued:
                per_article_fallbacks["amazon"] = "discontinued"
                discontinued_count += 1

            if asin_for_manifest:
                by_asin[asin_for_manifest] = {"fallbacks": per_article_fallbacks}

            _meta_re = re.compile(r"\s*[(（]\s*\d+\s*字\s*[)）]\s*$")
            if isinstance(data.get("narrative"), dict):
                for k in ("lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose"):
                    v = data["narrative"].get(k)
                    if isinstance(v, str):
                        v = _meta_re.sub("", v).strip()
                    # 生の ASIN コードは読者に意味がないので落とす (str/list 両対応)。
                    # how_to_choose は配列で来ることが多く、上の 字数メタ除去は
                    # str しか見ていなかったため配列側は素通りしていた。
                    v = _strip_inline_asin_codes(v)
                    if v is not None and k in data["narrative"]:
                        data["narrative"][k] = v
            for top_key in ("editorial_comment", "expert_take"):
                v = data.get(top_key)
                if isinstance(v, str):
                    data[top_key] = _meta_re.sub("", v).strip()

            # #3332 N3 消費側: 逐語一致検証はレンダリングされる最終テキストに対して
            # 行う必要があるため、narrative クリーンアップの後・template.render の前に呼ぶ。
            links_injected, links_dropped = _inject_internal_links(
                data, article_index, site_base_path
            )
            internal_links_injected_total += links_injected
            internal_links_dropped_total += links_dropped

            # #1107: enrichment + 価格 backfill 後の data から ScoreResult を 1 回だけ算出し、
            # 本文 (data["score"]) と frontmatter (_frontmatter_meta) で再利用する
            # (本文 / frontmatter 用の calculate_score 呼出を 2 回 → 1 回に削減)。
            # 注: _build_article_index 側は raw JSON 由来の cross-article カード用スコアを
            # 別途算出しており、価格 backfill 前の値を持つため再利用不可。
            fresh_sr: ScoreResult | None = None
            product_obj = data.get("product")
            if isinstance(product_obj, dict):
                raw_brand_for_score = product_obj.get("brand")
                if raw_brand_for_score:
                    try:
                        fresh_sr = calculate_score(
                            data,
                            normalize_brand(raw_brand_for_score),
                            asin=product_obj.get("asin"),
                        )
                    except Exception:
                        fresh_sr = None
            # #1111: テンプレへは data["score"] を直接渡す (旧 product.ivs_detail の
            # in-place mutation = _sync_ivs_for_render は廃止)。
            # #1124: product.ivs_score の上書きも廃止 (frontmatter は
            # _frontmatter_meta が fresh_sr を直接参照する)。残る副作用は
            # レビュー CTA URL の付与のみ。
            if fresh_sr is not None:
                data["score"] = _build_score_context(fresh_sr)
                if isinstance(product_obj, dict):
                    asin_for_review = product_obj.get("asin")
                    # 取り扱い終了 (snapshot status=gone) の dp ページは 404 なので
                    # レビュー CTA は付けない。
                    if asin_for_review and not amazon_discontinued:
                        product_obj["amazon_review_url"] = (
                            f"https://www.amazon.co.jp/dp/{asin_for_review}"
                            "/?tag=zefiransesu-22#customerReviews"
                        )
            # 年齢別発達段階目安データのマッピング
            target_age_raw = ""
            if isinstance(product_obj, dict):
                target_age_raw = product_obj.get("target_age")
            if not target_age_raw and isinstance(data.get("persona_fit"), dict):
                target_age_raw = data["persona_fit"].get("age_range")

            age_months = _parse_age_min_months(target_age_raw)
            development_stage = _get_development_stage(age_months, stages_data)
            if development_stage:
                data["development_stage"] = development_stage

            # Issue #515: link_report_flag macro が記事 URL 構築用に slug を参照する。
            data["slug"] = slug
            page_asin = _resolve_page_asin(data.get("product"), slug)
            # #1980: query_intent_map にこのページの主要検索意図があれば post.md.j2 の
            # cta_layout 分岐 (購入 CTA の掲載位置) に渡す。未検出なら None のままで
            # テンプレ側は従来位置に fallback する。
            if query_intent_map:
                data["cta_layout"] = query_intent_map.get(f"/products/{page_asin.lower()}/")

            # #2686 / #4964: 「どこで買える/在庫」記事型。ロールアウト日以降 かつ
            # 在庫文言が classify 可能な記事だけ新型 (決定的生成) を適用する。
            # 既存記事は is_stock_format_eligible が False を返すため一切変化しない。
            stock_title_override: str | None = None
            data["stock_where_to_buy"] = None
            if page_asin and where_to_buy_format.is_stock_format_eligible(
                page_asin, data.get("date"), stock_index,
            ):
                stock_obs = stock_status.resolve_stock(
                    page_asin, stock_index, per_asin_root=per_asin_root,
                )
                product_prices = (data.get("product") or {}).get("prices")
                purchase_options = stock_status.resolve_purchase_options(product_prices, stock_obs)
                product_name = (data.get("product") or {}).get("name") or data.get("title") or ""
                data["stock_where_to_buy"] = where_to_buy_format.build_stock_block(
                    product_name, stock_obs, purchase_options,
                    price_history_root=price_history_root,
                    price_watch_root=price_watch_history_root, asin=page_asin,
                )
                stock_title_override = where_to_buy_format.build_title(product_name, purchase_options)
                stock_title_applied += 1

            md_body = template.render(**data)
            # #4826 項目 4: 旧 <slug>.quality.json sidecar による draft 判定は廃止。
            # draft は --gate で**その場で評価した**スコアだけで決める (下記)。
            draft = False
            fm_meta = _frontmatter_meta(
                data, slug, draft, git_history, f,
                score_result=fresh_sr, query_intent_map=query_intent_map,
                canonical_overrides=canonical_overrides,
                stock_title_override=stock_title_override,
            )
            if fm_meta.get("cta_layout"):
                cta_layout_applied += 1
            post = frontmatter.Post(md_body, **fm_meta)

            out_file = dst_path / f"{slug}.md"
            out_file.write_text(frontmatter.dumps(post, width=10000), encoding="utf-8")
            tag = "  [DRAFT: low quality]" if draft else ""

            if args.gate and evaluate_article is not None:
                report = evaluate_article(
                    f, schema, out_file,
                    rakuten_idx=rakuten_matched_index,
                    yahoo_idx=yahoo_matched_index,
                )
                if args.min_score > 0 and report.total_score < args.min_score and not draft:
                    post.metadata["draft"] = True
                    out_file.write_text(frontmatter.dumps(post, width=10000), encoding="utf-8")
                    tag = f"  [DRAFT: score {report.total_score} < {args.min_score}]"
                else:
                    tag += f"  [score {report.total_score}]"

            legacy_tag = "  [legacy v3 fallback]" if legacy_v3 else ""
            print(f"Rendered: {out_file}{tag}{legacy_tag}")
            rendered += 1
        except Exception as e:
            print(f"Error processing {f}: {e}")

    # data/build_manifest.json の出力
    manifest_dir = pathlib.Path("data")
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "built_at": datetime.now().astimezone().isoformat(),
        "total": rendered,
        "summary": {
            "youtube": stats["youtube"],
            "news": stats["news"],
            "books": stats["books"],
            "omcha": stats["omcha"],
            # #677: score_calculator が記録した missing (file 不在) と empty
            # (file 有り API 0 hit) を分離して可視化。
            "media_exposure_metrics": get_media_exposure_metrics().as_dict(),
            # #3332 N3 消費側: internal_link_suggestions の注入/drop 集計。
            "internal_links": {
                "injected": internal_links_injected_total,
                "dropped": internal_links_dropped_total,
            },
            # #4007: Amazon 価格をどの観測レーンで上書きしたかの内訳。
            # none が増えたら日次レーン (price_watch) の停止を疑う。
            "price_overlay": price_overlay_stats,
        },
        # #677: per-ASIN の fallback breakdown。`gh run view` から個別 ASIN の
        # 状態をドリルダウン確認するために残す。
        "by_asin": by_asin,
    }
    try:
        (manifest_dir / "build_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"Failed to write build_manifest.json: {e}")

    # GITHUB_STEP_SUMMARY への出力
    import os
    summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_env:
        def _pct(val: int, total: int) -> str:
            return f"{val} ({val / total * 100:.1f}%)" if total > 0 else "0 (0.0%)"

        def _cov_pct(primary: int, fallback: int, total: int) -> str:
            return f"{(primary + fallback) / total * 100:.1f}%" if total > 0 else "0.0%"

        me = get_media_exposure_metrics().as_dict()
        me_total = me["total"] or rendered
        summary_md = f"""
## Build Manifest ({rendered} articles)

| Media Type | Primary (Jules) | Fallback (Cache) | Missing | Coverage |
| :--- | :---: | :---: | :---: | :---: |
| **YouTube** | {_pct(stats['youtube']['primary'], rendered)} | {_pct(stats['youtube']['fallback'], rendered)} | {_pct(stats['youtube']['missing'], rendered)} | {_cov_pct(stats['youtube']['primary'], stats['youtube']['fallback'], rendered)} |
| **News** | {_pct(stats['news']['primary'], rendered)} | {_pct(stats['news']['fallback'], rendered)} | {_pct(stats['news']['missing'], rendered)} | {_cov_pct(stats['news']['primary'], stats['news']['fallback'], rendered)} |
| **Books** | {_pct(stats['books']['primary'], rendered)} | {_pct(stats['books']['fallback'], rendered)} | {_pct(stats['books']['missing'], rendered)} | {_cov_pct(stats['books']['primary'], stats['books']['fallback'], rendered)} |
| **omcha** | {_pct(stats['omcha']['primary'], rendered)} | {_pct(stats['omcha']['fallback'], rendered)} | {_pct(stats['omcha']['missing'], rendered)} | {_cov_pct(stats['omcha']['primary'], stats['omcha']['fallback'], rendered)} |

### Score calculator media-exposure ({me_total} ASIN)

`missing` = per-ASIN cache file 不在 (fetch サイクル未到達) / `empty` = file 有り API 0 hit。Issue #677。

| Source | missing | empty |
| :--- | :---: | :---: |
| YouTube | {_pct(me['yt_missing'], me_total)} | {_pct(me['yt_empty'], me_total)} |
| News | {_pct(me['news_missing'], me_total)} | {_pct(me['news_empty'], me_total)} |
| omcha | {_pct(me['omcha_missing'], me_total)} | {_pct(me['omcha_empty'], me_total)} |
"""
        try:
            with open(summary_env, "a", encoding="utf-8") as sf:
                sf.write(summary_md)
        except Exception as e:
            print(f"Failed to write GITHUB_STEP_SUMMARY: {e}")

    msg = f"\nDone. {rendered} post(s) rendered."
    if discontinued_count:
        msg += f" {discontinued_count} rendered as Amazon 取り扱い終了 (snapshot status=gone)."
    if skipped_legacy:
        msg += f" {skipped_legacy} rendered as legacy v3 fallback (regenerate via Jules)."
    print(msg)
    print(f"cta_layout applied: {cta_layout_applied} page(s)")
    print(
        f"internal_links: {internal_links_injected_total} injected / "
        f"{internal_links_dropped_total} dropped"
    )
    print(
        f"price_overlay: {price_overlay_stats['price_watch']} price_watch / "
        f"{price_overlay_stats['per_asin']} per_asin / "
        f"{price_overlay_stats['none']} no observation "
        f"({price_overlay_stats['body_stale']} flagged 本文価格乖離, "
        f"{price_overlay_stats['unpriced_cleared']} 凍結価格を無価格化)"
    )
    print(f"stock_title (#2686): {stock_title_applied} page(s) rendered with 「どこで買える」format")


if __name__ == "__main__":
    main()
