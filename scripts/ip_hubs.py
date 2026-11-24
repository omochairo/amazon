"""キャラクター (IP) 別の束ねページ (navi-brain#211 の G1 試作)。

なぜ: 1 商品 1 記事の形では、アンパンマンやたまごっちのように記事が何十〜何百本ある
キャラクターでも、「どれを選べばいいか」を受けるページが無い (タグページは商品を
並べるだけで、GSC の 1 ページあたりクリックはサイトで最も低い)。一方、選び分けを
書いた特集ページは 1 商品記事の約 7 倍クリックされている (仕様 v2 §12.1)。
そこで特集と同じ型 (layout feature) で、キャラクターごとに
  - 年齢別・遊びの種類別の節 (データから決定的に作る)
  - 予算別の比較表 (年齢 hub と同じ部品)
  - 選び分けの文章 (data/hub_narratives/<key>.json。書き手が作り、validate_narrative を通ったものだけ)
を出す。

載せるのは記事のある商品だけ (出典検証済みの 1 商品記事へリンクで戻す)。
選び分けの文章は、ここで渡した事実 (記事の長所・短所・年齢・スコア) だけで書く。
外の事実 (キャラクターの歴史など) は書かない — 出典を付けられないため。

使い方:
    python scripts/ip_hubs.py prompt ip-anpanman > prompt.txt   # 書き手への依頼文
    python scripts/ip_hubs.py gate data/hub_narratives/ip-anpanman.json
build_category_hubs.py の run() が build_ip_hubs() を呼び、hugo/data/features/<key>.json を書く。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_overlay  # noqa: E402
import toy_type  # noqa: E402
from candidate_priority import load_character_terms  # noqa: E402

# キー → 表示名・character_terms.yaml の正規化名・URL。
# 選んだ基準 (仕様 v2 §12.3 L1): 記事 30 本以上かつ「選ぶ系」の需要語がある IP。
# G1 では 2 本だけ作り、K が読んでから広げる。
# series: 同じキャラクターの中のシリーズ (型違い・色違いを束ねる。先に書いたものから当てる)。
# accessory: 本体ではない付属品・関連グッズ (上位・年齢・種類の節と比較表に出さず、関連グッズの節にまとめる)。
# 語は _fold した商品名への部分一致。記事の商品名を数えて決めた (2026-11-24)。
IP_HUBS: dict[str, dict[str, Any]] = {
    "ip-anpanman": {
        "label": "アンパンマン", "character": "アンパンマン", "url": "/anpanman-toys/",
        "series": [
            {"label": "ブロックラボ", "terms": ["ブロックラボ"]},
            {"label": "ベビラボ", "terms": ["ベビラボ"]},
            {"label": "ことばずかん", "terms": ["ことばずかん"]},
            {"label": "おしゃべり・知育パソコン", "terms": ["パソコン", "タブレット"]},
            {"label": "天才脳パズル", "terms": ["天才脳"]},
            {"label": "くねくねチューブ", "terms": ["くねくね"]},
        ],
        "accessory": ["保護フィルム", "シール", "ステッカー"],
    },
    "ip-tamagotchi": {
        "label": "たまごっち", "character": "たまごっち", "url": "/tamagotchi-toys/",
        "series": [
            {"label": "たまごっちパラダイス", "terms": ["パラダイス", "paradise"]},
            # 「Original Tamagotchi Angel Carousel」はオリジナルの柄違いなので、オリジナルを先に当てる
            {"label": "オリジナル・初代", "terms": ["original", "オリジナル", "初代", "オリたま"]},
            {"label": "てんしっち", "terms": ["てんしっち"]},
            {"label": "ケーたま", "terms": ["ケーたま"]},
            {"label": "新種発見", "terms": ["新種発見"]},
        ],
        "accessory": ["シール", "ステッカー", "ストラップ", "キャリー", "ケース", "ヘアゴム",
                      "リフィル", "保護フィルム", "たまもりアクセ", "コレクタブルズ", "コレクティブルズ",
                      "pop!", "キーホルダー", "ハンディファン", "ネックストラップ"],
    },
}

# 上位・節に載せる知育スコアの下限 (0-5)。全ラインナップ節には下限をかけない。
IP_MIN_IVS = 3.4
IP_TOP_N = 12
AGE_SECTION_MAX = 4
TYPE_SECTION_MAX = 3
TYPE_SECTION_MIN = 2

# 年齢の節。年齢 hub (build_category_hubs.AGE_HUBS) と同じ区切り。
AGE_BUCKETS = [
    ("age-0", "0歳", 0, 12),
    ("age-1", "1歳", 12, 24),
    ("age-2", "2歳", 24, 36),
    ("age-3", "3歳", 36, 48),
    ("age-4", "4〜5歳", 48, 72),
    ("age-6", "6歳以上", 72, 216),
]

NARRATIVE_DIR = Path("data/hub_narratives")


def _fold(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-‐‑‒–—―・_.,/（）()]+", "", value)


def ip_text(raw: dict[str, Any]) -> str:
    """キャラクター判定に使う文字列。商品名・正式名・ブランドだけを見る。

    題や本文は見ない (「アンパンマン好きにも」のような別商品の言及で当たるため)。
    """
    p = raw.get("product") or {}
    return _fold(" ".join(str(p.get(k) or "") for k in ("name", "name_full", "brand")))


def matches_character(raw: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    text = ip_text(raw)
    # 「デュプロ・アンパンマン互換」のような互換品はキャラクター商品ではない (candidate_priority と同じ)
    if "互換" in text:
        return False
    return any(_fold(a) and _fold(a) in text for a in aliases)


def load_raw_articles(articles_dir: Path) -> dict[str, dict[str, Any]]:
    """{asin: 記事 JSON}。同じ ASIN が複数あれば日付の新しいファイルを使う。"""
    out: dict[str, dict[str, Any]] = {}
    from build_feature_lists import _is_article_json
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):  # *.seo.json などの付属ファイル
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        asin = (raw.get("product") or {}).get("asin") if isinstance(raw, dict) else None
        if asin:
            out[str(asin)] = raw
    return out


def _amazon_item(per_asin_dir: Path, asin: str) -> dict[str, Any] | None:
    try:
        d = json.loads((per_asin_dir / asin / "amazon.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    item = d.get("item") or d
    return item if isinstance(item, dict) else None


def _rank_key(rec):
    return (-(rec.ivs_100 or 0), rec.best_price or 10**9)


def _bucket(mm: int | None) -> str | None:
    if mm is None:
        return None
    for key, _label, lo, hi in AGE_BUCKETS:
        if lo <= mm < hi:
            return key
    return None


def _brief(rec, mm: int | None, types: list[str]) -> dict[str, Any]:
    return {
        "asin": rec.asin,
        "name": rec.name,
        "url_internal": f"/products/{rec.asin.lower()}/",
        "image": rec.image,
        "ivs_100": rec.ivs_100,
        # 商品ごとの小さな「Amazon で見る」(表示側で amazon_affiliate_url.html がタグを強制)
        "amazon_url": rec.amazon_url,
        "best_price": rec.best_price,
        "best_platform": rec.best_platform,
        "age_min_months": rec.age_min_months if rec.age_min_months is not None else mm,
        "types": [toy_type.label(t) for t in types],
    }


def series_of(name: str | None, hub: dict[str, Any]) -> str | None:
    text = _fold(name)
    for s in hub.get("series") or []:
        if any(_fold(t) in text for t in s["terms"]):
            return s["label"]
    return None


def is_accessory(name: str | None, hub: dict[str, Any]) -> bool:
    """付属品か。「特典ステッカー付き」のように本体に付いてくるものは付属品にしない。"""
    text = _fold(name)
    for t in hub.get("accessory") or []:
        ft = _fold(t)
        i = text.find(ft)
        while i >= 0:
            if not text.startswith("付", i + len(ft)):
                return True
            i = text.find(ft, i + 1)
    return False


def pick_distinct(recs, hub: dict[str, Any], limit: int, *, per_series: int = 1):
    """スコア順の recs から、色違い・同じシリーズが並ばないように limit 件選ぶ。

    同じシリーズは per_series 件まで、シリーズ外は名前のほぼ同じもの (別出品・色違い) を 1 件に。
    足りなければ外したものをスコア順に戻して埋める (件数を減らすより重なりを残す)。
    """
    import build_category_hubs as bch
    picked, held, keys = [], [], []
    used: Counter = Counter()
    for r in recs:
        if len(picked) >= limit:
            break
        s = series_of(r.name, hub)
        key = bch._name_key(r.name)
        # 同じ商品の別出品 (名前がほぼ同じ) はシリーズの内外を問わず 1 件に
        if (s and used[s] >= per_series) or bch._same_product(key, keys):
            held.append(r)
            continue
        if s:
            used[s] += 1
        keys.append(key)
        picked.append(r)
    if len(picked) < limit:
        picked.extend(held[:limit - len(picked)])
        picked.sort(key=_rank_key)
    return picked


def select_ip_records(records, raw_index: dict[str, dict[str, Any]], aliases: tuple[str, ...]):
    """IP に当たる記事の record をスコア順で返す (記事のある商品だけ)。"""
    pool = [r for r in records if r.asin in raw_index and matches_character(raw_index[r.asin], aliases)]
    pool.sort(key=_rank_key)
    return pool


def build_ip_payload(key: str, records, raw_index, mm_index, *, per_asin_dir: Path,
                     narrative_dir: Path = NARRATIVE_DIR, min_ivs: float = IP_MIN_IVS,
                     compare_builder=None, rules=None) -> tuple[list, dict[str, Any]]:
    """(上位 record の list, feature JSON に足す項目) を返す。"""
    hub = IP_HUBS[key]
    terms = load_character_terms()
    aliases = (hub["character"], *terms.get(hub["character"], ()))
    pool = select_ip_records(records, raw_index, aliases)
    rules = rules or toy_type.load_rules()
    types_of: dict[str, list[str]] = {}
    for rec in pool:
        raw = raw_index[rec.asin]
        types_of[rec.asin] = toy_type.classify_article(raw, _amazon_item(per_asin_dir, rec.asin))
    good_all = [r for r in pool if r.ivs_score is not None and r.ivs_score >= min_ivs]
    accessories = [r for r in good_all if is_accessory(r.name, hub)]
    good = [r for r in good_all if not is_accessory(r.name, hub)]
    top = pick_distinct(good, hub, IP_TOP_N, per_series=2)

    age_sections = []
    for bkey, label, lo, hi in AGE_BUCKETS:
        members = [r for r in good if _bucket(mm_index.get(r.asin)) == bkey]
        if not members:
            continue
        age_sections.append({
            "key": bkey, "label": label, "count": sum(1 for r in pool if _bucket(mm_index.get(r.asin)) == bkey),
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                      for r in pick_distinct(members, hub, AGE_SECTION_MAX)],
        })

    type_sections = []
    type_counts = Counter(t for r in pool for t in types_of[r.asin])
    for tkey in rules.keys:
        members = [r for r in good if tkey in types_of[r.asin]]
        if len(members) < TYPE_SECTION_MIN:
            continue
        type_sections.append({
            "key": tkey, "label": toy_type.label(tkey), "count": type_counts[tkey],
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                      for r in pick_distinct(members, hub, TYPE_SECTION_MAX)],
        })

    # シリーズの節: 同じシリーズの型違い・色違いを 1 か所で見比べる (仕様 v2 §12.3 L3 の受け皿)
    series_sections = []
    for s in hub.get("series") or []:
        members = [r for r in pool if series_of(r.name, hub) == s["label"] and not is_accessory(r.name, hub)]
        if len(members) < 2:
            continue
        series_sections.append({
            "label": s["label"], "count": len(members),
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin]) for r in members[:6]],
        })
    accessory_section = [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                         for r in pick_distinct(accessories, hub, 6)]

    stats = {
        "articles": len(pool),
        "scored_ok": len(good),
        "age_counts": {b[0]: sum(1 for r in pool if _bucket(mm_index.get(r.asin)) == b[0]) for b in AGE_BUCKETS},
        "type_counts": {toy_type.label(k): v for k, v in type_counts.most_common()},
        "in_stock": sum(1 for r in pool if r.best_price and not price_overlay.is_explicitly_unavailable(r.availability)),
    }
    extra: dict[str, Any] = {
        "label": hub["label"],
        "ip": hub["character"],
        "stats": stats,
        "age_sections": age_sections,
        "type_sections": type_sections,
        "series_sections": series_sections,
        "accessories": accessory_section,
    }
    if compare_builder is not None:
        table = compare_builder([r for r in pool if not is_accessory(r.name, hub)])
        if table:
            extra["compare_table"] = table
    shown = {r.asin for r in top}
    extra["lineup"] = [
        {"asin": r.asin, "name": r.name, "url_internal": f"/products/{r.asin.lower()}/",
         "ivs_100": r.ivs_100, "age_min_months": mm_index.get(r.asin)}
        for r in pool if r.asin not in shown
    ]
    narrative = load_narrative(narrative_dir / f"{key}.json")
    if narrative is not None:
        by_asin = {r.asin: r for r in pool}
        errors = validate_narrative(narrative, set(by_asin), {s["key"] for s in age_sections},
                                    names=[r.name or "" for r in pool],
                                    accessory_asins={r.asin for r in pool if is_accessory(r.name, hub)},
                                    choose_asins={r.asin for r in good})
        if errors:
            # 検査に落ちた文章は出さない (データの節だけのページにする)。記事が消えた・ASIN が
            # 変わった・スコアが下限を割ったときもここに来る。1 件だけ外すと本文の言及と食い違う
            # ので、文章ごと外して書き直しを待つ。理由はビルドのログへ
            print(f"[ip_hubs] WARNING {key}: narrative rejected: {errors[:5]}", file=sys.stderr)
        else:
            extra["narrative"] = _render_narrative(narrative, by_asin, mm_index, types_of)
    return top, extra


def _render_narrative(n: dict[str, Any], by_asin, mm_index, types_of) -> dict[str, Any]:
    """表示用に、文章の ASIN 参照へ商品の名前・URL・画像を差し込む。"""
    def card(asin):
        r = by_asin[asin]
        return _brief(r, mm_index.get(asin), types_of.get(asin, []))
    return {
        "lead": n["lead"],
        "choose": [{**card(c["asin"]), "when": c["when"], "why": c["why"]} for c in n["choose"]],
        "by_age": {a["age_key"]: a["text"] for a in n.get("by_age") or []},
        "series_tips": [
            {"title": s["title"], "text": s["text"], "items": [card(a) for a in s.get("asins") or []]}
            for s in n.get("series_tips") or []
        ],
        "not_for": list(n.get("not_for") or []),
        "faq": list(n.get("faq") or []),
        "generated_at": n.get("generated_at"),
    }


# --------------------------------------------------------------------------
# 選び分けの文章の検査 (仕様 v2 §12.3 R3)。「仕様を並べただけ」「データに無いこと」を落とす。
# --------------------------------------------------------------------------

_ASIN_RE = re.compile(r"\bB0[0-9A-Z]{8}\b")
# 本文は NFKC + casefold してから当てる (全角数字・全角記号も半角に寄る)
_YEN_RE = re.compile(r"[0-9,.]+\s*(万|千)?\s*円|[¥￥]\s*[0-9]|[一二三四五六七八九十百千万]+円")
_NUM_RE = re.compile(r"[0-9０-９]+(?:[.．][0-9０-９]+)?")
_AGE_TAIL_RE = re.compile(r"\s*(歳|才|ヶ月|か月|カ月|ヵ月|ケ月)")
# 書かせない言い切り・誇大表現 (景表法・Google の品質方針)。
_HYPE = ("絶対", "必ず", "最強", "No.1", "ナンバーワン", "間違いな", "100%", "誰でも")
# 価格の動きは本文に書かない (凍った文になる。金額はビルド時に描く・amazon#11867)。
_PRICE_WORDS = ("最安", "値下がり", "値上がり", "セール", "特価", "割引", "今が買い")
CHOOSE_MIN = 5
CHOOSE_MAX = 8
WHY_MIN_CHARS = 30
LEAD_MIN_CHARS = 120
LEAD_MAX_CHARS = 400
SIMILAR_MAX = 0.5


def load_narrative(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _grams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def _jaccard(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _texts(n: dict[str, Any]) -> list[str]:
    out = [str(n.get("lead") or "")]
    for c in n.get("choose") or []:
        out += [str(c.get("when") or ""), str(c.get("why") or "")]
    out += [str(a.get("text") or "") for a in n.get("by_age") or []]
    for s in n.get("series_tips") or []:
        out += [str(s.get("title") or ""), str(s.get("text") or "")]
    out += [str(x) for x in n.get("not_for") or []]
    for f in n.get("faq") or []:
        out += [str(f.get("q") or ""), str(f.get("a") or "")]
    return out


def _shape_errors(n: dict[str, Any]) -> list[str]:
    """表示 (_render_narrative) が読む形になっているか。崩れた JSON でビルドを落とさない。"""
    errs: list[str] = []
    if not isinstance(n.get("lead"), str):
        errs.append("lead must be a string")
    specs = {
        "choose": ("asin", "when", "why"),
        "by_age": ("age_key", "text"),
        "series_tips": ("title", "text"),
        "faq": ("q", "a"),
    }
    for field, keys in specs.items():
        v = n.get(field)
        if v is None:
            v = []
        if not isinstance(v, list):
            errs.append(f"{field} must be a list")
            continue
        for i, item in enumerate(v):
            if not isinstance(item, dict) or any(
                    not isinstance(item.get(k), str) or not item.get(k).strip() for k in keys):
                errs.append(f"{field}[{i}] must have non-empty {', '.join(keys)}")
            elif field == "series_tips" and not isinstance(item.get("asins") or [], list):
                errs.append(f"series_tips[{i}].asins must be a list")
    nf = n.get("not_for")
    if nf is not None and (not isinstance(nf, list) or any(not isinstance(x, str) for x in nf)):
        errs.append("not_for must be a list of strings")
    return errs


def validate_narrative(n: dict[str, Any], pool_asins: set[str], age_keys: set[str],
                       *, names: list[str] | None = None,
                       accessory_asins: set[str] | None = None,
                       choose_asins: set[str] | None = None) -> list[str]:
    """違反の一覧を返す (空なら合格)。choose_asins を渡すと、選び分けに出せる商品をそこに限る。"""
    errors: list[str] = []
    if not isinstance(n, dict):
        return ["not an object"]
    shape = _shape_errors(n)
    if shape:
        return shape  # 形が崩れていると以降の検査も表示も読めない
    lead = str(n.get("lead") or "")
    if not (LEAD_MIN_CHARS <= len(lead) <= LEAD_MAX_CHARS):
        errors.append(f"lead length {len(lead)} not in {LEAD_MIN_CHARS}-{LEAD_MAX_CHARS}")
    choose = n.get("choose") or []
    if not (CHOOSE_MIN <= len(choose) <= CHOOSE_MAX):
        errors.append(f"choose count {len(choose)} not in {CHOOSE_MIN}-{CHOOSE_MAX}")
    seen: set[str] = set()
    for i, c in enumerate(choose):
        asin = str(c.get("asin") or "")
        if asin not in pool_asins:
            errors.append(f"choose[{i}] asin {asin} is not in this hub")
        elif choose_asins is not None and asin not in choose_asins:
            errors.append(f"choose[{i}] asin {asin} is below the score floor")
        if asin in seen:
            errors.append(f"choose[{i}] asin {asin} duplicated")
        # 「どれを選ぶ」は本体の選び分け。付属品 (シール・ケース等) を入れると読者の問いに答えない
        if accessory_asins and asin in accessory_asins:
            errors.append(f"choose[{i}] asin {asin} is an accessory")
        seen.add(asin)
        if not str(c.get("when") or "").strip():
            errors.append(f"choose[{i}] when is empty")
        if len(str(c.get("why") or "")) < WHY_MIN_CHARS:
            errors.append(f"choose[{i}] why shorter than {WHY_MIN_CHARS}")
    # 分岐どうしが同じ言い回しなら選び分けになっていない
    whens = [str(c.get("when") or "") for c in choose]
    for i in range(len(whens)):
        for j in range(i + 1, len(whens)):
            if _jaccard(whens[i], whens[j]) >= SIMILAR_MAX:
                errors.append(f"choose[{i}] and choose[{j}] 'when' too similar")
    for a in n.get("by_age") or []:
        if a.get("age_key") not in age_keys:
            errors.append(f"by_age key {a.get('age_key')} has no section")
    for s in n.get("series_tips") or []:
        asins = s.get("asins") or []
        if len(asins) < 2:
            errors.append(f"series_tips '{s.get('title')}' compares fewer than 2 products")
        for a in asins:
            if a not in pool_asins:
                errors.append(f"series_tips asin {a} is not in this hub")
    if not (n.get("not_for") or []):
        errors.append("not_for is empty")
    allowed_numbers = {unicodedata.normalize("NFKC", m) for nm in (names or [])
                       for m in _NUM_RE.findall(nm)}
    for t in _texts(n):
        if _ASIN_RE.search(t):
            errors.append(f"raw ASIN in text: {t[:40]}")
        norm = unicodedata.normalize("NFKC", t).casefold()
        if _YEN_RE.search(norm):
            errors.append(f"price amount in text: {t[:40]}")
        for w in _HYPE:
            if w.casefold() in norm:
                errors.append(f"hype word '{w}': {t[:40]}")
        for w in _PRICE_WORDS:
            if w in norm:
                errors.append(f"price-movement word '{w}': {t[:40]}")
        # 数字は年齢・月齢と、商品名に同じ数字として出るものだけ (渡していない数字 = 作った事実)
        for m in _NUM_RE.finditer(norm):
            num = m.group(0)
            if _AGE_TAIL_RE.match(norm, m.end()):
                continue
            if num in allowed_numbers:
                continue
            errors.append(f"number '{num}' not from ages or product names: {t[:40]}")
    return errors


# --------------------------------------------------------------------------
# 書き手への依頼文
# --------------------------------------------------------------------------

PROMPT_HEAD = """あなたは知育玩具の比較サイト「おもちゃいろ 知育玩具比較ナビ」の編集者です。
キャラクター「{label}」のおもちゃを選ぶ親のために、下の【事実】だけを使って「選び分け」の文章を JSON で書いてください。

ルール (守らないと自動検査で全部捨てられます):
- 【事実】に無いことは書かない。キャラクターの歴史・発売年・人気の度合い・他社の評判など、外の知識は使わない
- 金額・「最安」「値下がり」は書かない (価格は別に表示される)。「絶対」「必ず」「最強」「No.1」も使わない
- 数字は年齢・月齢と、商品名に入っている数字だけ。記事数や割合は書かない (ページが自動で出す)
- ASIN のコードを本文に書かない (asin 欄にだけ書く)
- 商品の紹介を 1 つずつ並べるのではなく、「どういう子・家庭なら、どれを選ぶか」の分かれ目を書く。長所だけでなく短所 (cons) も選び分けの理由に使う
- 「choose」は {cmin}〜{cmax} 件。accessory が true の商品 (シール・ケースなどの付属品) は入れない。それぞれ違う商品で、when (どんな子・どんな場面に) は互いに違う切り口にする (年齢・遊び方・置き場所・遊ぶ人数・電池の有無など)
- 「series_tips」は、同じシリーズの型違い・似た名前の商品があるときだけ 0〜3 件。違いが【事実】から言えるものだけ
- 「by_age」は【事実】の年齢の節にあるキーだけ。各 1〜2 文
- 「not_for」は、このキャラクターのおもちゃを選ばない方がいい場合や、選ぶときの注意 1〜3 件
- 「faq」は親が実際に迷う質問 2〜4 件。答えは【事実】から言えることだけ
- 文体は「です・ます」。読者に向けて書く。調べた経緯や「事実によると」は書かない

出力は次の形の JSON だけ (前後に説明を付けない):
{{"lead": "{lmin}〜{lmax} 字", "choose": [{{"asin": "...", "when": "...", "why": "{wmin} 字以上"}}], "by_age": [{{"age_key": "age-1", "text": "..."}}], "series_tips": [{{"title": "...", "text": "...", "asins": ["...", "..."]}}], "not_for": ["..."], "faq": [{{"q": "...", "a": "..."}}]}}

【事実】
"""


def build_prompt(key: str, records, raw_index, mm_index, *, per_asin_dir: Path, top_k: int = 40) -> str:
    hub = IP_HUBS[key]
    top, extra = build_ip_payload(key, records, raw_index, mm_index, per_asin_dir=per_asin_dir,
                                  narrative_dir=Path("/nonexistent"))
    terms = load_character_terms()
    aliases = (hub["character"], *terms.get(hub["character"], ()))
    pool = select_ip_records(records, raw_index, aliases)
    pool = [r for r in pool if r.ivs_score is not None and r.ivs_score >= IP_MIN_IVS][:top_k]
    products = []
    for r in pool:
        raw = raw_index[r.asin]
        p = raw.get("product") or {}
        axes = r.ivs_axes or {}
        products.append({
            "asin": r.asin,
            "name": r.name,
            "brand": p.get("brand"),
            "series": series_of(r.name, hub),
            "accessory": is_accessory(r.name, hub),
            "age_section": _bucket(mm_index.get(r.asin)),
            "target_age": p.get("target_age"),
            "types": [toy_type.label(t) for t in toy_type.classify_article(raw, _amazon_item(per_asin_dir, r.asin))],
            "score_100": r.ivs_100,
            "score_axes": {k: axes.get(k) for k in ("education", "longevity", "safety", "cost_performance")},
            "price_band": _price_band(r.best_price),
            "pros": list(p.get("pros") or [])[:4],
            "cons": list(p.get("cons") or [])[:3],
        })
    facts = {
        "character": hub["label"],
        "age_sections": [{"key": s["key"], "label": s["label"]} for s in extra["age_sections"]],
        "products": products,
    }
    head = PROMPT_HEAD.format(label=hub["label"], cmin=CHOOSE_MIN, cmax=CHOOSE_MAX,
                              lmin=LEAD_MIN_CHARS, lmax=LEAD_MAX_CHARS, wmin=WHY_MIN_CHARS)
    return head + json.dumps(facts, ensure_ascii=False, indent=1) + "\n"


def _price_band(price: int | None) -> str | None:
    if not price:
        return None
    if price < 3000:
        return "3,000円未満"
    if price < 5000:
        return "3,000〜5,000円"
    if price < 10000:
        return "5,000〜10,000円"
    return "10,000円以上"


def check_against_data(key: str, n: dict[str, Any], records, raw_index, mm_index, *,
                       per_asin_dir: Path) -> list[str]:
    """ビルド (build_ip_payload) と同じ条件で、文章を今の記事データに当てる。"""
    hub = IP_HUBS[key]
    _top, extra = build_ip_payload(key, records, raw_index, mm_index, per_asin_dir=per_asin_dir,
                                   narrative_dir=Path("/nonexistent"))
    terms = load_character_terms()
    pool = select_ip_records(records, raw_index, (hub["character"], *terms.get(hub["character"], ())))
    good = {r.asin for r in pool if r.ivs_score is not None and r.ivs_score >= IP_MIN_IVS}
    return validate_narrative(n, {r.asin for r in pool}, {s["key"] for s in extra["age_sections"]},
                              names=[r.name or "" for r in pool],
                              accessory_asins={r.asin for r in pool if is_accessory(r.name, hub)},
                              choose_asins=good)


def _load_context(articles_dir: Path, per_asin_dir: Path, raw_root: Path):
    import build_category_hubs as bch
    from build_feature_lists import _dedupe_by_asin, load_articles, overlay_current_prices
    records = _dedupe_by_asin(load_articles(articles_dir))
    overlay_current_prices(records, per_asin_dir, raw_root=raw_root)
    return records, load_raw_articles(articles_dir), bch.build_min_months_index(articles_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("prompt")
    p1.add_argument("key", choices=list(IP_HUBS))
    p2 = sub.add_parser("gate")
    p2.add_argument("path", type=Path)
    for p in (p1, p2):
        p.add_argument("--articles-dir", default=Path("data/articles"), type=Path)
        p.add_argument("--per-asin-dir", default=Path("data/raw/per_asin"), type=Path)
        p.add_argument("--raw-root", default=Path("data/raw"), type=Path)
    args = ap.parse_args(argv)
    records, raw_index, mm_index = _load_context(args.articles_dir, args.per_asin_dir, args.raw_root)
    if args.cmd == "prompt":
        sys.stdout.write(build_prompt(args.key, records, raw_index, mm_index, per_asin_dir=args.per_asin_dir))
        return 0
    key = args.path.stem
    if key not in IP_HUBS:
        print(f"unknown hub key {key}", file=sys.stderr)
        return 2
    n = load_narrative(args.path)
    if n is None:
        print("cannot read narrative", file=sys.stderr)
        return 2
    errors = check_against_data(key, n, records, raw_index, mm_index, per_asin_dir=args.per_asin_dir)
    for e in errors:
        print("NG", e)
    print("OK" if not errors else f"{len(errors)} errors")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
