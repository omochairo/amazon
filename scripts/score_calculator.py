"""score_calculator.py

知育スコア (0-100 / ivs_score 0-5) を 6 要素から論理計算する。
review データが取得できないため review_signal は使わず、
media_exposure (YouTube/news 件数 + omcha.jp 関連記事マッチ度) と
multi_market (楽天/Yahoo 取扱) で代替する。

Public API:
    calculate(article, brand, asin=None) -> ScoreResult

配点:
    brand_tier       25   (S=25 / A=20 / B=15 / C=10 / D=5)
    safety_cert      10   (ST=10 / 海外認証=8 / その他=5 / なし=0)
    age_fit          10   (明確範囲=10 / 下限のみ=8 / 記載=5 / なし=0)
    edu_value        15   (STEM/言語/運動/想像 検出分野数 0→0..4→15)
    media_exposure   15   (yt+news+omcha 関連記事マッチ度トップスコアで配点)
    multi_market     10   (rakuten+yahoo=10 / 片方=5 / なし=0)
    price_value      15   (円/想定使用年数 で tier 化)
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Optional

from brand_normalizer import NormalizedBrand


@dataclass
class ScoreResult:
    total_100: int
    ivs_score: float
    breakdown: dict[str, int] = field(default_factory=dict)
    rationale: list[str] = field(default_factory=list)


# tier ベース基礎点 (max 25): S=25 / A=20 / B=15 / C=10 / D=5
# 「掲載に値する玩具メーカー = 一定の下駄」を表現
# 2026-05-19: 35→25 に戻す。raw_total 上限 100 に揃え、cap 100 が頻発する設計ミスを解消
_TIER_POINTS = {"S": 25, "A": 20, "B": 15, "C": 10, "D": 5}

# 安全性 tier floor (max 10): 国内 C tier までは ST マーク玩具組合加入企業 = 安全試験前提
# S=10 / A=9 / B=8 / C=6 / D=0
_SAFETY_FLOOR = {"S": 10, "A": 9, "B": 8, "C": 6, "D": 0}

# 知育 tier floor (max 15): 玩具メーカーである以上 0 にはしない
# S=5 / A=4 / B=3 / C=2 / D=0
_EDU_FLOOR = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 0}

_EDU_DOMAINS = {
    "STEM": ["STEM", "プログラミング", "ロボット", "数学", "科学", "実験", "論理", "パズル", "ブロック"],
    "言語": ["言語", "ことば", "語彙", "文字", "絵本", "英語", "ひらがな", "カタカナ", "読み聞かせ"],
    "運動": ["運動", "手指", "協調運動", "バランス", "身体", "微細運動", "握力", "指先"],
    "想像": ["想像", "創造", "ごっこ", "ロールプレイ", "見立て", "クリエイティブ", "ままごと"],
}

# v5: edu_domains フィールド要素数 → tier floor への追加点
# n=4 → +10 cap15 / n=3 → +8 / n=2 → +6 / n=1 → +3 / n=0 → +0
_EDU_DOMAIN_BONUS = {0: 0, 1: 3, 2: 6, 3: 8, 4: 10}
_VALID_EDU_DOMAINS = {"STEM", "言語", "運動", "想像"}


# SEO 向け自然文 reason の語彙テーブル
# 内部式 (tier=A -> 20/25) ではなく、商品の魅力を伝える平叙文で書き、
# ブランド名 / 認証名 / 知育分野などの SEO 上意味のあるキーワードを含める。
_BRAND_TIER_TEMPLATE = {
    "S": "{name}は知育玩具業界でトップクラスの信頼を得ているブランドで、設計品質と長年の実績に裏付けられた安心感があります。",
    "A": "{name}は知育玩具メーカーとして広く知られたブランドで、商品の安全性と教育的配慮に定評があります。",
    "B": "{name}は知育・玩具分野で確かな実績を持つブランドで、信頼できる商品設計が期待できる水準です。",
    "C": "{name}は玩具市場で着実に商品展開を続けるブランドで、基本的な品質基準を満たした製品づくりが行われています。",
    # D tier はブランド名を出さず「商品本来の特徴で評価する」スタンスに振る
    "D": "知育玩具市場で流通する商品で、ブランドの知名度より商品本来の機能・素材・設計で評価するのに適したカテゴリです。",
}


def _brand_tier_score(brand: NormalizedBrand) -> tuple[int, str]:
    pt = _TIER_POINTS.get(brand.tier, 5)
    template = _BRAND_TIER_TEMPLATE.get(brand.tier, _BRAND_TIER_TEMPLATE["D"])
    # D tier はテンプレ内に {name} がない (固定文) ため format しても安全
    return pt, template.format(name=brand.canonical or "メーカー")


def _safety_score(brand: NormalizedBrand, product: dict) -> tuple[int, str]:
    # tier floor (国内 C tier までは ST マーク玩具組合加入企業の前提) — 死守
    floor = _SAFETY_FLOOR.get(brand.tier, 0)

    # v5: product.certifications がフィールドとして存在すれば field-driven 配点
    # (空配列も「Jules が明示的に none と判定」とみなして fallback には落とさない)
    pcerts_raw = product.get("certifications")
    field_driven = isinstance(pcerts_raw, list)

    if field_driven:
        up = {str(c).upper() for c in pcerts_raw if c}
        # ST / CE / EN71 / ASTM は同等扱い (海外メーカー強化方針)
        if "ST" in up or (up & {"CE", "EN71", "ASTM"}):
            bonus = 2
        elif "食品衛生法" in {str(c) for c in pcerts_raw if c}:
            bonus = 1
        else:
            bonus = 0
        pts = min(10, floor + bonus)
    else:
        # 後方互換フォールバック: brand.safety_default のみで判定
        certs = list(brand.safety_default or [])
        up = {c.upper() for c in certs if c}
        bonus = 1 if "ST" in up else (1 if up & {"CE", "EN71", "ASTM"} else 0)
        pts = min(10, floor + bonus)

    if "ST" in up:
        reason = "STマーク取得済みで、日本玩具協会の安全基準をクリアしています。お子さまが口に入れたり投げたりしても比較的安心して使える設計です。"
    elif up & {"CE", "EN71", "ASTM"}:
        cert_jp = ", ".join(sorted(up & {"CE", "EN71", "ASTM"}))
        reason = f"{cert_jp} 認証を取得しており、海外の玩具安全基準に適合しています。素材や塗料の安全性が第三者検証されています。"
    elif brand.tier in ("S", "A", "B"):
        reason = f"{brand.canonical or 'メーカー'}は玩具安全規格に準拠した製造体制を持つブランドのため、商品の安全性は標準水準を満たしていると考えられます。"
    elif brand.tier == "C":
        reason = "玩具メーカー組合加入企業が手がける商品のため、出荷前の基本的な安全試験が前提となっています。"
    else:
        reason = "STマーク等の認証情報は商品本文と Amazon 商品ページの素材・対象年齢欄で確認できます。お子さまの月齢に合わせて選ぶ際の参考にしてください。"
    return pts, reason


def _age_fit_score(product: dict) -> tuple[int, str, Optional[tuple[int, int]]]:
    """対象年齢の明確さスコア (0-10) を返す。

    玩具安全基準 / ST マーク制度では「対象年齢 = 下限のみ明示 (X歳以上)」が
    業界標準フォーマット。これは「安全に遊び始められる年齢」を伝える正式表記で、
    上限の有無は「子どもの成長次第で長く遊べる」ことを意味するため、買い手視点では
    範囲表記 (X歳〜Y歳) と同等の情報量を持つ。よって両者を満点 (10) とする。
    上限の有無は longevity 軸 (brand_tier + media_exposure) で別途評価済。

    旧設計 (#1107 follow-up 以前) は下限のみ表記を AG=8 にしていたため、78.6%
    (422/537 件) の記事が AG=10 に到達せず、サイト全体で実質 -2 raw の常時
    ペナルティを抱えていた。
    """
    # v5: product.target_age を優先 (Jules 出力の構造化フィールド)
    # 後方互換: なければ age / age_band もフォールバック対象
    raw = str(
        product.get("target_age")
        or product.get("age")
        or product.get("age_band")
        or ""
    )
    if not raw.strip():
        # 対象年齢の記載が一切ない: 安全に与えられる下限が分からない真の情報不足
        return 4, "対象年齢の詳細は商品ページに準じます。お子さまの発達段階に合わせて選んでください。", None

    # 範囲: "3歳〜7歳" / "6ヶ月〜2歳"
    # 月単位の場合は age_range タプルでは歳換算 (months/12, 小数切上げ) を返す
    def _to_years(n: int, unit: str) -> int:
        return max(1, (n + 11) // 12) if "ヶ月" in unit else n

    m = re.search(r"(\d+)\s*(歳|才|ヶ月)\s*[〜~\-]\s*(\d+)\s*(歳|才|ヶ月)", raw)
    if m:
        lo_n, lo_u, hi_n, hi_u = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        lo, hi = _to_years(lo_n, lo_u), _to_years(hi_n, hi_u)
        span = hi - lo
        label = f"{lo_n}{lo_u}〜{hi_n}{hi_u}"
        if span >= 5:
            return 10, f"対象年齢は{label}と幅広く、成長段階に合わせて長く遊べる設計です。きょうだいで共用しやすいのも魅力です。", (lo, hi)
        return 10, f"対象年齢は{label}に明確に設定されており、その年齢帯のお子さまに最適化された遊び方ができます。", (lo, hi)

    # 下限のみ: "6歳以上" / "3歳〜" / "12ヶ月から"
    # 玩具安全基準の標準表記。下限が parseable なら買い手は「安全に与えられる年齢」を
    # 判断できる十分な情報を持つため満点とする (#1107 follow-up)。
    m = re.search(r"(\d+)\s*(歳|才|ヶ月)\s*(?:〜|~|から|以上|\+)", raw)
    if m:
        n, u = int(m.group(1)), m.group(2)
        lo = _to_years(n, u)
        return 10, f"{n}{u}以上の対象年齢が明示されており、安全に遊び始められる年齢が明確です。上限の指定はなく、興味があれば長く楽しめる設計です。", (lo, lo + 5)

    # 数値が抽出できない textual mention のみ ("幼児向け" 等)
    return 6, f"対象年齢の記載は『{raw[:20]}』です。お子さまの月齢・発達状況に合わせてご検討ください。", None


_EDU_DOMAIN_PHRASES = {
    "STEM": "STEM・論理思考",
    "言語": "言語・読み書き",
    "運動": "手指・微細運動",
    "想像": "想像力・創造性",
}


def _edu_value_score(article: dict, brand: NormalizedBrand) -> tuple[int, str]:
    floor = _EDU_FLOOR.get(brand.tier, 0)

    # v5: product.edu_domains を優先 (Jules 出力の構造化フィールド)
    product = article.get("product") or {}
    edu_raw = product.get("edu_domains")
    if isinstance(edu_raw, list):
        hit = [d for d in edu_raw if d in _VALID_EDU_DOMAINS]
        n = len(set(hit))
        bonus = _EDU_DOMAIN_BONUS.get(min(n, 4), 0)
    else:
        # 後方互換フォールバック: tags/keywords ブロブ検出
        blob = " ".join(
            [
                " ".join(article.get("tags") or []),
                " ".join(article.get("keywords") or []),
                str(article.get("title", "")),
                str(article.get("meta_description", "")),
            ]
        )
        hit = [d for d, kws in _EDU_DOMAINS.items() if any(k in blob for k in kws)]
        n = len(hit)
        bonus = round(min(10, n * 2.5))
    pts = min(15, floor + bonus)

    if n >= 2:
        phrases = "・".join(_EDU_DOMAIN_PHRASES.get(d, d) for d in hit)
        reason = f"{phrases}と、複数の知育領域に同時に働きかける構成です。一つの遊びで多面的な発達を促せる点が評価できます。"
    elif n == 1:
        phrase = _EDU_DOMAIN_PHRASES.get(hit[0], hit[0])
        reason = f"{phrase}の領域に重点を置いた設計で、その分野の学習効果が特に期待できる商品です。"
    elif brand.tier in ("S", "A", "B"):
        reason = f"{brand.canonical or 'メーカー'}は知育志向の商品ラインを多く展開するブランドのため、子どもの発達を意識した遊び方ができる設計です。"
    else:
        # D tier かつ知育分野キーワード未検出: 商品本文への誘導で締める
        reason = "本商品の知育的な遊び方・期待される発達効果は、本記事の使い方解説と Amazon 商品ページの仕様欄を組み合わせてご判断ください。"
    return pts, reason


@dataclass
class _MediaExposureMetrics:
    """Counter for ``_media_exposure_score`` data availability per ASIN.

    Distinguishes between "fetch cycle未到達" (file absent) and "API returned 0
    hits" (file present but empty). Issue #677.
    """

    total: int = 0
    yt_missing: int = 0
    yt_empty: int = 0
    news_missing: int = 0
    news_empty: int = 0
    omcha_missing: int = 0
    omcha_empty: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "yt_missing": self.yt_missing,
            "yt_empty": self.yt_empty,
            "news_missing": self.news_missing,
            "news_empty": self.news_empty,
            "omcha_missing": self.omcha_missing,
            "omcha_empty": self.omcha_empty,
        }


_METRICS = _MediaExposureMetrics()


def get_media_exposure_metrics() -> _MediaExposureMetrics:
    """Snapshot of the missing/empty counters accumulated since import (or last
    ``reset_media_exposure_metrics`` call). Consumed by build_post manifest
    output (Issue #677)."""
    return _METRICS


def reset_media_exposure_metrics() -> None:
    """Reset the module-level metrics counters. Useful for tests."""
    global _METRICS
    _METRICS = _MediaExposureMetrics()


def _count_items(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("items") if isinstance(data, dict) else data
    return len(items) if isinstance(items, list) else 0


def _file_state(path: pathlib.Path) -> str:
    """Return ``"missing"`` / ``"empty"`` / ``"present"`` for an items-file
    cache path. Reused by ``_media_exposure_score`` to distinguish
    fetch-cycle-未到達 from API-returned-0-hits without re-reading the file."""
    if not path.exists():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "missing"
    items = data.get("items") if isinstance(data, dict) else data
    if isinstance(items, list) and items:
        return "present"
    return "empty"


# omcha.jp 関連記事スコアの閾値 (iro/v2 の 0..100 正規化スケール、Issue #6103)。
#
# v1 は上限のない素の合計値で、スケールがクエリの語数に依存していた
# (実タグキーワードでの実測レンジ 1..504)。閾値 30/15/10 はその尺度のもの
# なので v2 にそのまま持ち込めない。実タグキーワード 250 件で v1 (client
# min_score=20) と同じ配点分布になる点を選び直した:
#
#   配点   v1 (30/15/10)   v2 (18/12)
#   4 点       68.8%          63.2%
#   3 点       19.2%          24.8%
#   0 点       12.0%          12.0%
#
# 3 段目 (v1 の `>= 10` → 2 点) は削除した。fetch 側が min_score=20 で
# 足切りしてから書いているため、0 < om_top < 20 のキャッシュは存在せず
# v1 でも到達不能な枝だった (実測 250 件でも該当 0 件)。v2 でも
# min_score=12 = _OM_TOP_MATCH なので同じく到達不能になる。
_OM_TOP_STRONG = 18
_OM_TOP_MATCH = 12


def _omcha_top_score(path: pathlib.Path) -> int:
    """Return the highest match score among omcha.jp related articles cached
    at ``path`` (file written by build_post._attach_omcha_related). 0 when
    missing or empty."""
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0
    top = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        s = it.get("score")
        if isinstance(s, (int, float)) and s > top:
            top = int(s)
    return top


def _media_exposure_score(
    asin: str, brand: NormalizedBrand, repo_root: pathlib.Path
) -> tuple[int, str]:
    is_overseas = brand.region not in ("JP", "unknown", "")
    floor = 5 if is_overseas else 0
    if not asin:
        reason = "ASIN 情報が不足しているため、メディア露出は中立評価としています。"
        return floor, reason
    d = repo_root / "data" / "raw" / "per_asin" / asin
    yt_path = d / "youtube.json"
    news_path = d / "news.json"
    omcha_path = d / "omcha_related.json"
    yt_state = _file_state(yt_path)
    news_state = _file_state(news_path)
    omcha_state = _file_state(omcha_path)
    _METRICS.total += 1
    if yt_state == "missing":
        _METRICS.yt_missing += 1
    elif yt_state == "empty":
        _METRICS.yt_empty += 1
    if news_state == "missing":
        _METRICS.news_missing += 1
    elif news_state == "empty":
        _METRICS.news_empty += 1
    if omcha_state == "missing":
        _METRICS.omcha_missing += 1
    elif omcha_state == "empty":
        _METRICS.omcha_empty += 1
    yt = _count_items(yt_path)
    nw = _count_items(news_path)
    om_top = _omcha_top_score(omcha_path)
    yt_p = 6 if yt >= 3 else 3 if yt >= 1 else 0
    nw_p = 5 if nw >= 2 else 2 if nw >= 1 else 0
    om_p = (
        4 if om_top >= _OM_TOP_STRONG
        else 3 if om_top >= _OM_TOP_MATCH
        else 0
    )
    raw = yt_p + nw_p + om_p
    pts = min(15, max(floor, raw))

    # 主要な露出を 1 文目に、補助露出 + 締めを 2 文目に分けて読みやすくする
    primary = ""
    if yt >= 3:
        primary = "YouTube に複数の紹介動画があり、実際の遊び方や子どもの反応を映像で確認できます"
    elif yt >= 1:
        primary = "YouTube に紹介動画があり、遊び方のイメージを掴みやすい商品です"
    elif om_top >= _OM_TOP_STRONG:
        primary = "本サイトの編集記事と商品コンセプトが強くマッチしており、関連レビューを横断して特徴を比較できます"
    elif nw >= 1:
        primary = "ニュース・記事での取り上げ事例があり、市場の評価情報を参照できます"
    elif om_top >= _OM_TOP_MATCH:
        primary = "本サイト内に関連レビューがあり、近いコンセプトの商品と比較しながら選べます"

    extras = []
    if primary and "YouTube" not in primary and (yt >= 1):
        extras.append("YouTube にも紹介動画あり")
    if primary and "ニュース" not in primary and (nw >= 1):
        extras.append("関連ニュースの取り上げ実績あり")
    if primary and "本サイト" not in primary and (om_top >= _OM_TOP_MATCH):
        extras.append("本サイト内の関連レビューあり")

    if primary:
        if extras:
            reason = f"{primary}。加えて{('・'.join(extras))}と、購入前の比較検討がしやすい情報量が揃っています。"
        else:
            reason = f"{primary}。"
    elif is_overseas:
        reason = "海外発のブランドで日本語の紹介事例は限定的ですが、商品本来のスペックは本文の詳細解説でご確認いただけます。"
    else:
        reason = "本記事執筆時点では関連メディアでの紹介事例が限定的なため、商品本来の特徴は本文の使い方解説をご参照ください。"
    return pts, reason


_MARKET_CACHE: Optional[dict[str, set[str]]] = None


def _load_market(repo_root: pathlib.Path) -> dict[str, set[str]]:
    global _MARKET_CACHE
    if _MARKET_CACHE is not None:
        return _MARKET_CACHE
    out = {"rakuten": set(), "yahoo": set()}
    for key in ("rakuten", "yahoo"):
        p = repo_root / "data" / "raw" / f"{key}_matched.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                a = it.get("asin") or it.get("matched_asin") or it.get("target_asin")
                if a:
                    out[key].add(a)
    _MARKET_CACHE = out
    return out


def _multi_market_score(
    asin: str, brand: NormalizedBrand, repo_root: pathlib.Path
) -> tuple[int, str]:
    is_overseas = brand.region not in ("JP", "unknown", "")
    floor = 5 if is_overseas else 3
    if not asin:
        return floor, "ASIN 情報が不足しているため、取扱マーケットは中立評価としています。"
    mm = _load_market(repo_root)
    if not mm["rakuten"] and not mm["yahoo"]:
        return floor, "本記事執筆時点では Amazon を主要な購入経路としています。楽天・Yahoo! の取扱状況は変動するため最新情報をご確認ください。"
    r, y = asin in mm["rakuten"], asin in mm["yahoo"]
    if r and y:
        return 10, "Amazon・楽天市場・Yahoo!ショッピングの3サイトで取扱があり、ポイント還元・セール・送料を比較して最適な購入先を選べます。"
    if r:
        return max(floor, 7), "Amazon と楽天市場で取扱があり、楽天ポイント還元やセール時の価格差を比較しながら購入できます。"
    if y:
        return max(floor, 7), "Amazon と Yahoo!ショッピングで取扱があり、PayPayポイントやキャンペーン併用で実質価格を抑えられる場合があります。"
    if is_overseas:
        return floor, "海外ブランドのため正規流通は限定的で、Amazon が現時点で最も入手しやすいルートです。"
    return floor, "現時点では Amazon が主要な購入経路です。楽天・Yahoo! の取扱は時期により変動するため、本文の価格比較セクションをご参照ください。"


def _price_value_score(
    product: dict, age_range: Optional[tuple[int, int]]
) -> tuple[int, str]:
    price = product.get("best_price") or product.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return 5, "本記事執筆時点での価格情報を取得中です。最新の価格は本文の比較セクションでご確認ください。"
    years = max(1.0, (age_range[1] - age_range[0] + 1) if age_range else 3.0)
    yp = price / years
    price_yen = f"¥{int(price):,}"
    yp_yen = f"¥{int(yp):,}"
    if yp <= 1000:
        pts = 15
        reason = f"価格は{price_yen}で、対象年齢の幅から見ると年間{yp_yen}相当。長く遊べる前提で考えるとコストパフォーマンスは非常に高い水準です。"
    elif yp <= 2000:
        pts = 12
        reason = f"価格は{price_yen}で、年間コスト換算で{yp_yen}前後。ブランド品質と遊び込める期間を踏まえると良好なコストパフォーマンスです。"
    elif yp <= 5000:
        pts = 8
        reason = f"価格は{price_yen}で、年間換算{yp_yen}相当。やや投資感はありますが、機能性とブランド信頼度を考慮すると妥当な価格帯です。"
    elif yp <= 10000:
        pts = 5
        reason = f"価格は{price_yen}とやや高めの設定です。ギフトや特別な機会の選択肢として、品質・耐久性を重視する方に向いた価格帯です。"
    else:
        pts = 2
        reason = f"価格は{price_yen}とプレミアム帯です。コレクション目的・ギフト・専門用途など、特定の目的にこだわる方に向いた商品です。"
    return pts, reason


def calculate(
    article: dict,
    brand: NormalizedBrand,
    asin: Optional[str] = None,
    repo_root: Optional[pathlib.Path] = None,
) -> ScoreResult:
    product = article.get("product") or {}
    asin = asin or product.get("asin") or ""
    repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent

    bt, br = _brand_tier_score(brand)
    sf, sr = _safety_score(brand, product)
    ag, ar, age_range = _age_fit_score(product)
    ev, er = _edu_value_score(article, brand)
    me, mr = _media_exposure_score(asin, brand, repo_root)
    mm, mmr = _multi_market_score(asin, brand, repo_root)
    pv, pr = _price_value_score(product, age_range)

    # 各要素配点: brand_tier(25) + safety(10) + age(10) + edu(15) + media(15) + market(10) + price(15) = max 100
    #
    # 線形リマップ: total = max(50, min(100, round(50 + max(0, raw - 30) * 5/7)))
    #   raw 30 -> 50 / raw 50 -> 64 / raw 65 -> 75 / raw 80 -> 86 / raw 100 -> 100
    #
    # 設計意図:
    #   - 上限は raw 100 → total 100 を維持 (edu_value=15 を満たした S-tier 完璧記事の希少枠)
    #   - 下限は実データ最弱記事 (raw≈30 = D-tier ノーブランド + 限定情報) を total 50 に対応付け、
    #     旧 (50 + raw*0.5) で 65 に張り付いていた下端を 50 まで開放して分布を広げる
    #   - 中域は (raw - 30) * 5/7 で線形に按分。raw 80 = 86 のように高評価記事に十分な解像度を維持
    #
    # 履歴:
    #   2026-05-19: 旧 (raw_cap=110, 係数 0.5) → (raw_cap=100, 係数 0.4) に変更。raw cap 飽和は
    #               解消したが係数まで下げたため total max が 90 に圧縮される副作用発生。
    #   #1107 follow-up A: raw_cap=100 のまま係数 0.5 に戻し total max=100 を回復。ただし
    #               実データ下限 raw≈30 が total 65 で頭打ちになり分布 65-99 と狭かった。
    #   #1107 follow-up B (本変更): 下端を raw 30→50 に再マッピングし分布を 50-99 へ拡張。
    #               上端は変更せず (ユーザー要望: 上限は現状維持で十分)。
    raw_total = max(0, min(100, bt + sf + ag + ev + me + mm + pv))
    total = max(50, min(100, round(50 + max(0, raw_total - 30) * 5 / 7)))
    return ScoreResult(
        total_100=total,
        ivs_score=round(total / 20.0, 2),
        breakdown={
            "brand_tier": bt,
            "safety_cert": sf,
            "age_fit": ag,
            "edu_value": ev,
            "media_exposure": me,
            "multi_market": mm,
            "price_value": pv,
        },
        rationale=[br, sr, ar, er, mr, mmr, pr],
    )


def compute_ivs_axes(breakdown: dict) -> dict:
    """Project a 7-element ``breakdown`` to the 4 axes shown in /5 score UI.

    Mirrors the scaling used in ``build_post._sync_ivs_for_render`` so the
    article detail page, list cards (#589), and feature pages all agree.
    Axes are clamped to 2.0-5.0 ("floor 2.0 because a real toy is never
    a 0/5 axis"). Shared so build_post / build_feature_lists don't drift.
    """
    def _scale(raw: float, max_v: float) -> float:
        return round(2.0 + (raw / max_v) * 3.0, 1)
    longevity_norm = (breakdown["brand_tier"] / 25 + breakdown["media_exposure"] / 15) / 2
    return {
        "education": _scale(breakdown["edu_value"], 15),
        "safety": _scale(breakdown["safety_cert"], 10),
        "cost_performance": _scale(breakdown["price_value"], 15),
        "longevity": round(2.0 + longevity_norm * 3.0, 1),
    }


def _cli() -> None:
    import glob
    from brand_normalizer import normalize

    files = sorted(glob.glob("data/articles/*.json"))
    files = [
        f
        for f in files
        if not f.endswith((".enrichment.json", ".quality.json", ".seo.json"))
    ]
    print(f"{'ASIN':12} {'score':>5} {'/100':>5}  brand[tier]   breakdown")
    for f in files:
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        product = d.get("product") or {}
        b = normalize(product.get("brand"))
        m = re.search(r"(B0[A-Z0-9]{8})", f)
        asin = m.group(1) if m else ""
        r = calculate(d, b, asin=asin)
        bd = r.breakdown
        bd_str = f"BT:{bd['brand_tier']} SF:{bd['safety_cert']} AG:{bd['age_fit']} EV:{bd['edu_value']} ME:{bd['media_exposure']} MK:{bd['multi_market']} PV:{bd['price_value']}"
        print(
            f"{asin:12} {r.ivs_score:>5} {r.total_100:>5}  {b.canonical}[{b.tier}]   {bd_str}"
        )

    m = get_media_exposure_metrics()
    if m.total:
        print(
            "score_calc media-exposure: "
            f"yt_missing={m.yt_missing}/{m.total} (empty={m.yt_empty}), "
            f"news_missing={m.news_missing}/{m.total} (empty={m.news_empty}), "
            f"omcha_missing={m.omcha_missing}/{m.total} (empty={m.omcha_empty}). "
            "missing=fetch サイクル未到達 / empty=API で 0 hit"
        )


if __name__ == "__main__":
    _cli()
