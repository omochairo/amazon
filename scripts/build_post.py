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
import html
import json
import pathlib
import re
import urllib.parse
from datetime import datetime
from typing import Any

import frontmatter
import jinja2

from brand_normalizer import normalize as normalize_brand
from build_feature_lists import PRICE_BANDS
from score_calculator import (
    ScoreResult,
    calculate as calculate_score,
    compute_ivs_axes,
    get_media_exposure_metrics,
    reset_media_exposure_metrics,
)


SUFFIX_SKIP = (".enrichment", ".seo", ".quality")


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
    for f in src_path.glob("*.json"):
        if f.stem.endswith(SUFFIX_SKIP):
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


def _has_japanese(text: str) -> bool:
    return any(
        "぀" <= c <= "ヿ" or "一" <= c <= "鿿"
        for c in (text or "")
    )


def _highlight_snippet_ok(snippet: str) -> bool:
    """True when the snippet reads like prose a reader benefits from, not a
    digit-dense parts list or a stub. Used to keep the highlights honest."""
    s = (snippet or "").strip()
    if len(s) < 30:
        return False
    if any(m in s for m in _HIGHLIGHT_JUNK_MARKERS):
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
# Issue #1072 Phase 3-B (2026-05-31): jan_unknown 58 ASIN を救済するため
#   price band を [0.5, 2.0] → [0.4, 2.5] に、coverage を 0.7 → 0.5 に緩和。
#   dry-run (scripts/analyze_threshold_relaxation.py) で +25 件救済を確認、
#   FP の主因は閾値ではなく Mamimami Home 系 search_keyword の品質問題
#   (別 Issue で対処予定)。relax_both preset 採用。
_MARKET_PRICE_BAND_LOW = 0.4   # Amazon 価格の 40% 未満は除外
_MARKET_PRICE_BAND_HIGH = 2.5  # Amazon 価格の 250% 超は除外
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
_MARKET_COVERAGE_RATIO = 0.5
# search_keyword を title overlap 判定するときに、汎用すぎて根拠にならない語
_MARKET_GENERIC_TOKENS = frozenset({
    "おもちゃ", "知育玩具", "プレゼント", "誕生日", "ギフト",
    "木製", "木のおもちゃ", "セット", "玩具",
})

# 型番ハイフン揺れを吸収 (例: 'RD-6' vs 'RD−6' / 'EH-2310' vs 'EH−2310').
# title vs kw の substring 判定で false negative を防ぐ。Issue #1140 で
# B09BQMCSFL (kw 'RD-6' vs title 'RD−6') が descriptor 不一致と誤判定された対応。
_HYPHEN_VARIANTS_RE = re.compile(r"[‐‑‒–—―−ー]")
# 中黒 (半角 ﾟ・ U+FF65 / 全角 ・ U+30FB / 半角 ･ U+FF65) を 1 文字に統一。
# 'トイ・ストーリー' vs 'トイ･ストーリー' を同一視。
_MIDDOT_VARIANTS_RE = re.compile(r"[･・]")


def _normalize_for_match(s: str) -> str:
    if not s:
        return s
    s = html.unescape(s)  # '&amp;' → '&' 等の HTML entity を吸収
    s = _HYPHEN_VARIANTS_RE.sub("-", s)
    s = _MIDDOT_VARIANTS_RE.sub("・", s)
    return s


def _is_model_number(token: str) -> bool:
    """ASCII 英数の型番トークン (E3209 / E0328 等) か判定する。

    別 SKU 誤マッチ検出用。>=1 英字 + >=2 数字 + ASCII[英数ハイフン]のみ を満たす
    ものだけ型番とみなす。RD-6 のような 1 桁や 2025 (数字のみ) / Lon-Bi (数字無し)
    は除外し、明確なカタログ型番だけを対象にして false-trip を避ける。
    """
    if not re.fullmatch(r"[A-Za-z0-9\-]+", token):
        return False
    if not any(c.isalpha() for c in token):
        return False
    return sum(c.isdigit() for c in token) >= 2


def _descriptor_hits_title(descriptor: str, title_norm: str, title_tokens_norm: list) -> bool:
    """Issue #1140: descriptor token が title に「実質的に」出現するか。

    1. 正規化後の substring 一致 (タッチペン → 'タッチペン...' を含む title)
    2. title token が descriptor の substring (タッチペン付 ⊃ タッチペン)
       → suffix 表記揺れ (付/版/セット) を吸収
    3. 先頭/末尾の punctuation を剥がして再判定 ('-Switch' → 'Switch')
    """
    d = _normalize_for_match(descriptor)
    if d in title_norm:
        return True
    for tt in title_tokens_norm:
        if len(tt) >= 3 and tt in d:
            return True
    d_stripped = d.strip("-_/.,;:!?()[]{}<>")
    if d_stripped and d_stripped != d and len(d_stripped) >= 3 and d_stripped in title_norm:
        return True
    return False


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

    # 型番ガード: search_keyword に ASCII 型番 (E3209 等) があるのに matched title に
    # 一つも存在しなければ別 SKU の誤マッチとして弾く。同ブランド別商品
    # (Hape レジカウンター E3209 vs ファーマーズマーケットの食べ物セット) が
    # カテゴリ語「ままごと」一致だけで通過し、quality_gate の reseller-pricing
    # check を誤発火させる事故 (B0CDTQWRN1) を source で断つ。
    model_tokens = [t for t in kw_tokens if _is_model_number(t)]
    if model_tokens:
        title_compact = re.sub(r"[-\s]", "", _normalize_for_match(title)).upper()
        if not any(
            re.sub(r"[-\s]", "", _normalize_for_match(m)).upper() in title_compact
            for m in model_tokens
        ):
            return False

    meaningful = [t for t in kw_tokens if t not in _MARKET_GENERIC_TOKENS]
    if not meaningful:
        return True  # 区別語が無ければ cross-search 側 median band の選出を尊重
    title_norm = _normalize_for_match(title)
    hits = sum(1 for t in meaningful if _normalize_for_match(t) in title_norm)
    threshold = 2 if len(meaningful) >= 2 else 1
    if hits < threshold:
        return False
    # 絶対 hits 数を満たしても、meaningful 全体に占める割合が低いマッチは
    # シリーズ違い/別モデルの誤マッチが多いため verified=False に格下げ。
    if hits / len(meaningful) < _MARKET_COVERAGE_RATIO:
        return False
    # Issue #1140: 非ブランド descriptor 一致を必須化。
    # search_keyword は extract_search_keyword で「brand head (先頭 1-2 token) +
    # descriptor」の構造になる。Mamimami Home のようなカテゴリ横断ブランドだと
    # brand head だけが title と一致して通過し、descriptor (ティッシュ/積み木/楽器)
    # が別商品とずれていても FP として救済されてしまう (relax_both で 4 件確認)。
    # kw と matched title の共通 leading token prefix を brand head とみなし、
    # それ以外の descriptor から最低 1 token は title に含まれることを要求する。
    title_tokens = [t for t in re.split(r"\s+", title) if t]
    prefix_len = 0
    for kt, tt in zip(kw_tokens, title_tokens):
        if _normalize_for_match(kt) == _normalize_for_match(tt):
            prefix_len += 1
        else:
            break
    if prefix_len >= 1:
        descriptor = [
            t for t in kw_tokens[prefix_len:]
            if len(t) >= 2 and t not in _MARKET_GENERIC_TOKENS
        ]
        if descriptor:
            title_tokens_norm = [_normalize_for_match(t) for t in title_tokens if len(t) >= 2]
            if not any(_descriptor_hits_title(d, title_norm, title_tokens_norm) for d in descriptor):
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
    score_result: ScoreResult | None = None,
) -> dict[str, Any]:
    title = data.get("title", "No Title")
    variants = data.get("title_variants") or []
    if variants and isinstance(variants[0], dict) and variants[0].get("title"):
        title = variants[0]["title"]
    description = data.get("meta_description_optimized") or data.get("meta_description", "")
    
    product = data.get("product") or {}
    asin = product.get("asin")
    if not asin:
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
    article_index = _build_article_index(src_path)
    site_base_path = _site_base_path(pathlib.Path(args.hugo_config))
    git_history = _load_git_history(src_path)

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

    rendered = 0
    skipped_legacy = 0

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
            # フォールバック実行前の Jules 由来データの存在有無を記録
            has_jules_yt = bool(data.get("youtube_embeds"))
            has_jules_news = bool(data.get("news"))
            has_jules_books = bool(data.get("books"))
            has_jules_omcha = bool(data.get("omcha_related"))

            _merge(data, _load_optional_json(src_path / f"{slug}.enrichment.json"), ENRICHMENT_KEYS)
            _merge(data, _load_optional_json(src_path / f"{slug}.seo.json"), SEO_KEYS)
            _backfill_amazon_badges(data, raw_amazon_index, per_asin_root)
            _attach_market_prices(data, rakuten_matched_index, yahoo_matched_index)
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

            if asin_for_manifest:
                by_asin[asin_for_manifest] = {"fallbacks": per_article_fallbacks}

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
                    if asin_for_review:
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
            md_body = template.render(**data)
            draft = _quality_draft(slug, src_path, args.min_score)
            post = frontmatter.Post(
                md_body,
                **_frontmatter_meta(data, slug, draft, git_history, f, score_result=fresh_sr),
            )

            out_file = dst_path / f"{slug}.md"
            out_file.write_text(frontmatter.dumps(post, width=10000), encoding="utf-8")
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
    if skipped_legacy:
        msg += f" {skipped_legacy} rendered as legacy v3 fallback (regenerate via Jules)."
    print(msg)


if __name__ == "__main__":
    main()
