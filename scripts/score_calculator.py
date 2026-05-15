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


# tier ベース基礎点 (max 35): S=35 / A=28 / B=22 / C=17 / D=10
# 「掲載に値する玩具メーカー = 一定の下駄」を表現
_TIER_POINTS = {"S": 35, "A": 28, "B": 22, "C": 17, "D": 10}

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


def _brand_tier_score(brand: NormalizedBrand) -> tuple[int, str]:
    pt = _TIER_POINTS.get(brand.tier, 10)
    return pt, f"brand_tier={brand.tier}({brand.canonical}) -> {pt}/35"


def _safety_score(brand: NormalizedBrand, product: dict) -> tuple[int, str]:
    # tier floor (国内 C tier までは ST マーク玩具組合加入企業の前提)
    floor = _SAFETY_FLOOR.get(brand.tier, 0)
    certs = list(brand.safety_default or [])
    pcerts = product.get("certifications") or []
    if isinstance(pcerts, list):
        certs += [str(c) for c in pcerts]
    up = {c.upper() for c in certs if c}
    # 認証ボーナス: ST 明記なら +1 (D tier には大きく効く)
    bonus = 1 if "ST" in up else (1 if up & {"CE", "EN71", "ASTM"} else 0)
    pts = min(10, floor + bonus)
    return pts, f"safety=tier_floor({brand.tier}:{floor})+cert({sorted(up) or '-'}:{bonus}) -> {pts}/10"


def _age_fit_score(product: dict) -> tuple[int, str, Optional[tuple[int, int]]]:
    raw = str(
        product.get("target_age")
        or product.get("age")
        or product.get("age_band")
        or ""
    )
    if not raw.strip():
        # Jules がフィールドを出さない場合の中立扱い (Phase 3 で改善)
        return 5, "age=未取得 -> 5/10(中立)", None
    m = re.search(r"(\d+)\s*[〜~\-]\s*(\d+)\s*(?:歳|才)", raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return 10, f"age={lo}-{hi}歳 -> 10/10", (lo, hi)
    m = re.search(r"(\d+)\s*(?:歳|才)\s*(?:〜|~|から|以上|\+)", raw)
    if m:
        lo = int(m.group(1))
        return 8, f"age={lo}歳〜 -> 8/10", (lo, lo + 5)
    return 5, f"age=記載のみ('{raw[:15]}') -> 5/10", None


def _edu_value_score(article: dict, brand: NormalizedBrand) -> tuple[int, str]:
    # tier floor (玩具メーカーは 0 にしない)
    floor = _EDU_FLOOR.get(brand.tier, 0)
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
    # キーワード加点: 1分野=+2.5, 2=+5, 3=+7.5, 4=+10 (max 10)
    bonus = round(min(10, n * 2.5))
    pts = min(15, floor + bonus)
    return pts, f"edu=tier_floor({brand.tier}:{floor})+domains[{','.join(hit) or '-'}]({n}/4:+{bonus}) -> {pts}/15"


def _count_items(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("items") if isinstance(data, dict) else data
    return len(items) if isinstance(items, list) else 0


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
    # 海外ブランドは日本語メディア露出が少ないのが普通 -> 中立 5 を保証
    is_overseas = brand.region not in ("JP", "unknown", "")
    floor = 5 if is_overseas else 0
    if not asin:
        return floor, f"media=asin不明 -> {floor}/15"
    d = repo_root / "data" / "raw" / "per_asin" / asin
    yt = _count_items(d / "youtube.json")
    nw = _count_items(d / "news.json")
    # omcha.jp 関連記事マッチ度: トップスコアで段階配点 (books 枠を置き換え)
    # API score 内訳例: タイトル完全一致=15 / タグ・カテゴリ完全一致=8 / サブワード=6
    # 1件でも 30 超え = 商品コンセプトが omcha 編集記事と強くマッチ
    om_top = _omcha_top_score(d / "omcha_related.json")
    yt_p = 6 if yt >= 3 else 3 if yt >= 1 else 0
    nw_p = 5 if nw >= 2 else 2 if nw >= 1 else 0
    om_p = 4 if om_top >= 30 else 3 if om_top >= 15 else 2 if om_top >= 10 else 0
    raw = yt_p + nw_p + om_p
    pts = min(15, max(floor, raw))
    tag = f"[海外floor={floor}]" if is_overseas and floor > raw else ""
    return pts, f"media=yt:{yt}({yt_p}) news:{nw}({nw_p}) omcha:{om_top}({om_p}){tag} -> {pts}/15"


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
    # 海外ブランドは正規流通が限定的で楽天/Yahoo に出にくい -> 中立 5 を保証
    is_overseas = brand.region not in ("JP", "unknown", "")
    floor = 5 if is_overseas else 3
    if not asin:
        return floor, f"market=asin不明 -> {floor}/10"
    mm = _load_market(repo_root)
    if not mm["rakuten"] and not mm["yahoo"]:
        return floor, f"market=matchedデータなし -> {floor}/10(中立)"
    r, y = asin in mm["rakuten"], asin in mm["yahoo"]
    if r and y:
        return 10, "market=楽天+Yahoo -> 10/10"
    if r or y:
        return max(floor, 7), f"market={'楽天' if r else 'Yahoo'}のみ -> {max(floor,7)}/10"
    return floor, f"market=Amazonのみ(matched無) -> {floor}/10(中立)"


def _price_value_score(
    product: dict, age_range: Optional[tuple[int, int]]
) -> tuple[int, str]:
    # 価格フィールドは best_price (= 最安マーケット価格) を優先、なければ price
    price = product.get("best_price") or product.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return 5, "price=未取得 -> 5/15(中立)"
    years = max(1.0, (age_range[1] - age_range[0] + 1) if age_range else 3.0)
    yp = price / years
    pts = (
        15
        if yp <= 1000
        else 12
        if yp <= 2000
        else 8
        if yp <= 5000
        else 5
        if yp <= 10000
        else 2
    )
    return pts, f"price=¥{price:.0f}/{years:.0f}yr=¥{yp:.0f}/yr -> {pts}/15"


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

    # 各要素配点: brand_tier(35) + safety(10) + age(10) + edu(15) + media(15) + market(10) + price(15) = max 110
    # 係数 0.5 でリマップ: D tier を 50 寄りに落としつつ tier floor で S/A/B を底上げ。
    # マップ式: final = max(50, min(100, 50 + raw * 0.5))
    #   raw 0 -> 50, raw 40 -> 70, raw 70 -> 85, raw 100+ -> 100
    raw_total = max(0, min(110, bt + sf + ag + ev + me + mm + pv))
    total = max(50, min(100, round(50 + raw_total * 0.5)))
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


def _cli() -> None:
    import glob
    from brand_normalizer import normalize

    files = sorted(glob.glob("data/articles/*.json"))
    files = [
        f
        for f in files
        if not f.endswith((".enrichment.json", ".quality.json", ".seo.json"))
    ]
    print(f"{'ASIN':12} {'jules':>5} {'new':>5} {'/100':>5}  brand[tier]   breakdown")
    for f in files:
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        product = d.get("product") or {}
        b = normalize(product.get("brand"))
        m = re.search(r"(B0[A-Z0-9]{8})", f)
        asin = m.group(1) if m else ""
        r = calculate(d, b, asin=asin)
        jules = product.get("ivs_score", "?")
        bd = r.breakdown
        bd_str = f"BT:{bd['brand_tier']} SF:{bd['safety_cert']} AG:{bd['age_fit']} EV:{bd['edu_value']} ME:{bd['media_exposure']} MK:{bd['multi_market']} PV:{bd['price_value']}"
        print(
            f"{asin:12} {str(jules):>5} {r.ivs_score:>5} {r.total_100:>5}  {b.canonical}[{b.tier}]   {bd_str}"
        )


if __name__ == "__main__":
    _cli()
