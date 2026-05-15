"""score_calculator.py

知育スコア (0-100 / ivs_score 0-5) を 6 要素から論理計算する。
review データが取得できないため review_signal は使わず、
media_exposure (YouTube/news/books 件数) と multi_market
(楽天/Yahoo 取扱) で代替する。

Public API:
    calculate(article, brand, asin=None) -> ScoreResult

配点:
    brand_tier       25   (S=25 / A=20 / B=15 / C=10 / D=5)
    safety_cert      10   (ST=10 / 海外認証=8 / その他=5 / なし=0)
    age_fit          10   (明確範囲=10 / 下限のみ=8 / 記載=5 / なし=0)
    edu_value        15   (STEM/言語/運動/想像 検出分野数 0→0..4→15)
    media_exposure   15   (per_asin/<ASIN>/{yt,news,books}.json items 数)
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


_TIER_POINTS = {"S": 25, "A": 20, "B": 15, "C": 10, "D": 5}

_EDU_DOMAINS = {
    "STEM": ["STEM", "プログラミング", "ロボット", "数学", "科学", "実験", "論理", "パズル", "ブロック"],
    "言語": ["言語", "ことば", "語彙", "文字", "絵本", "英語", "ひらがな", "カタカナ", "読み聞かせ"],
    "運動": ["運動", "手指", "協調運動", "バランス", "身体", "微細運動", "握力", "指先"],
    "想像": ["想像", "創造", "ごっこ", "ロールプレイ", "見立て", "クリエイティブ", "ままごと"],
}


def _brand_tier_score(brand: NormalizedBrand) -> tuple[int, str]:
    pt = _TIER_POINTS.get(brand.tier, 5)
    return pt, f"brand_tier={brand.tier}({brand.canonical}) -> {pt}/25"


def _safety_score(brand: NormalizedBrand, product: dict) -> tuple[int, str]:
    certs = list(brand.safety_default or [])
    pcerts = product.get("certifications") or []
    if isinstance(pcerts, list):
        certs += [str(c) for c in pcerts]
    up = {c.upper() for c in certs if c}
    if "ST" in up:
        return 10, f"safety=ST -> 10/10"
    if up & {"CE", "EN71", "ASTM"}:
        return 8, f"safety=海外認証({sorted(up)}) -> 8/10"
    if up:
        return 5, f"safety=その他({sorted(up)}) -> 5/10"
    return 0, "safety=なし -> 0/10"


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


def _edu_value_score(article: dict) -> tuple[int, str]:
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
    pts = [0, 4, 8, 12, 15][min(n, 4)]
    return pts, f"edu=[{','.join(hit) or '-'}] ({n}/4) -> {pts}/15"


def _count_items(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("items") if isinstance(data, dict) else data
    return len(items) if isinstance(items, list) else 0


def _media_exposure_score(asin: str, repo_root: pathlib.Path) -> tuple[int, str]:
    if not asin:
        return 0, "media=asin不明 -> 0/15"
    d = repo_root / "data" / "raw" / "per_asin" / asin
    yt = _count_items(d / "youtube.json")
    nw = _count_items(d / "news.json")
    bk = _count_items(d / "books.json")
    yt_p = 6 if yt >= 3 else 3 if yt >= 1 else 0
    nw_p = 5 if nw >= 2 else 2 if nw >= 1 else 0
    bk_p = 4 if bk >= 2 else 2 if bk >= 1 else 0
    pts = min(15, yt_p + nw_p + bk_p)
    return pts, f"media=yt:{yt}({yt_p}) news:{nw}({nw_p}) books:{bk}({bk_p}) -> {pts}/15"


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


def _multi_market_score(asin: str, repo_root: pathlib.Path) -> tuple[int, str]:
    if not asin:
        return 3, "market=asin不明 -> 3/10(中立)"
    mm = _load_market(repo_root)
    # rakuten/yahoo_matched は全 ASIN をカバーしない (fetch 対象限定) ため、
    # データなし = penalty ではなく中立 3 点とする
    if not mm["rakuten"] and not mm["yahoo"]:
        return 3, "market=matchedデータなし -> 3/10(中立)"
    r, y = asin in mm["rakuten"], asin in mm["yahoo"]
    if r and y:
        return 10, "market=楽天+Yahoo -> 10/10"
    if r or y:
        return 7, f"market={'楽天' if r else 'Yahoo'}のみ -> 7/10"
    return 3, "market=Amazonのみ(matched無) -> 3/10(中立)"


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
    ev, er = _edu_value_score(article)
    me, mr = _media_exposure_score(asin, repo_root)
    mm, mmr = _multi_market_score(asin, repo_root)
    pv, pr = _price_value_score(product, age_range)

    # raw は 0-100 範囲だが、実データで 10-65 に偏るため最終表示用に再マップ。
    # 最低 50 を保証 (掲載商品の暗黙下限), 100 で上限 cap。
    # マップ式: final = max(50, min(100, 50 + raw * 0.7))
    #   raw 0  -> 50,  raw 30 -> 71,  raw 60 -> 92,  raw 70+ -> 100
    raw_total = max(0, min(100, bt + sf + ag + ev + me + mm + pv))
    total = max(50, min(100, round(50 + raw_total * 0.7)))
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
