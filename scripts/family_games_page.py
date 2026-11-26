"""家族で遊べるゲーム（年齢別）のページ /family-games/ (navi-brain#216 案 A)。

build_category_hubs.py から呼ばれ、hugo/data/features/family-games.json を作る。
テンプレートは hugo/layouts/partials/family_games_page.html (カードは xmas_card.html を共用)。

なぜこの形か (#216 の 1〜3 章):
  - 「すごろく」「カルタ」などの頭の語は別の意味の検索が多く、お正月におもちゃを買う語は数百しかない
  - 需要があるのは年齢別のゲームの語 (ボードゲーム 3〜5歳・知育かるた) と、お正月遊びの情報の語
  - そこで一年中置くページを年齢別のゲームで作り、12/1〜1/15 だけお正月遊びの節を上に出す
    (時期の判定は閲覧時ではなくビルド時。配信ビルドは毎日走る)

方針は xmas_page.py と同じ: 文章は作らない。数字・声・記事にある仕様だけを並べ、無い項目は出さない。
"""
from __future__ import annotations

import re
import statistics
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xmas_page  # noqa: E402
from xmas_page import PlayType  # noqa: E402

PAGE_KEY = "family-games"
PAGE_URL = "/family-games/"
TOP_N = 12            # 年齢帯ごとの件数
MIN_ITEMS = 6         # これに届かない年齢帯は出さない
BUDGET_MAX = 10000    # クリスマスページと同じ上限
MAX_PER_BRAND = 2
KIND_CAP = 4          # 年齢帯の中で 1 つの種類はここまで (12 件の 3 分の 1)
MAX_AGE_MONTHS = 156  # 13 歳以上向け (大人のゲーム) は出さない

# 年齢帯。1 商品は対象年齢の下限で 1 つの帯にだけ入る (年齢 hub と同じ考え方。帯どうしで同じ商品を並べない)。
# lo / phrase は順位の理由 (xmas_page._age_phrase) に渡す。3 歳の帯は 2 歳からの物も入れる。
BANDS: tuple[dict[str, Any], ...] = (
    {"key": "age3", "label": "3歳", "phrase": "3歳", "min": 24, "max": 48, "lo": 36},
    {"key": "age4", "label": "4〜5歳", "phrase": "4歳", "min": 48, "max": 72, "lo": 48},
    {"key": "school", "label": "小学生", "phrase": "小学生", "min": 72, "max": MAX_AGE_MONTHS, "lo": 72},
)

# ゲームの種類。上から順に見て、最初に当たった種類にする (「ことわざカードかるた」はかるた)。
# kw は正規表現。商品名とタグ (NFKC・小文字) に当てる。
GAME_KINDS: tuple[PlayType, ...] = (
    PlayType("karuta", "🎴", "かるた", (r"カルタ|かるた|百人一首|歌留多",)),
    # 版元「すごろくや」のゲームは双六ではないので外す
    PlayType("sugoroku", "🎲", "すごろく", (r"すごろく(?!や)|双六|スゴロク",)),
    PlayType("traditional", "🎍", "昔遊び",
             (r"福笑い|ふくわらい|独楽|コマ回し|こま回し|糸引きこま|逆立ちこま|けん玉|剣玉|ケンダマ|けんだま"
              r"|だるま落とし|ダルマ落とし|羽子板|羽根つき|凧",)),
    PlayType("action", "🧱", "バランス・アクション",
             (r"バランスゲーム|ジェンガ|積み木崩し|ワニワニ|黒ひげ|アクションゲーム",)),
    PlayType("card", "🃏", "カードゲーム", (r"カードゲーム|トランプ|\buno\b|ウノ",)),
    PlayType("board", "♟️", "ボードゲーム",
             (r"ボードゲーム|ボード ゲーム|テーブルゲーム|アナログゲーム|パーティーゲーム|人生ゲーム",)),
)
_KIND_RE = tuple((k, re.compile(k.kw[0])) for k in GAME_KINDS)

# 遊ぶものでない物・それだけでは遊べない物 (トレカのパック・収納・拡張・飾り)
_EXCLUDE_RE = re.compile(
    r"ブースター|拡張|デッキ|カードホルダー|コレクションファイル|スリーブ|ストレージ|デュエル・マスターズ|デュエマ"
    r"|デュエキング|ポケモンカードゲーム|ワンピースカード|one piece カード|tcg|飾り|節句")

BUDGETS = (("u1000", "〜1,000円", 1000), ("u3000", "〜3,000円", 3000), ("u5000", "〜5,000円", 5000))


def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).lower()


def _text(raw: dict[str, Any]) -> str:
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    tags = [t for t in (raw.get("tags") or []) if isinstance(t, str)]
    return _norm(" ".join([str(product.get("name") or ""), *tags]))


def kind_of(raw: dict[str, Any]) -> PlayType | None:
    """ゲームの種類。ゲームでない・遊べない物 (_EXCLUDE_RE) は None。"""
    text = _text(raw)
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    if _EXCLUDE_RE.search(_norm(product.get("name"))):
        return None
    for kind, pat in _KIND_RE:
        if pat.search(text):
            return kind
    return None


# ---------------------------------------------------------------------------
# 人数・遊ぶ時間 (記事の technical_specs に書いてあるものだけ)
# ---------------------------------------------------------------------------

_DASH = r"\s*[〜~\-－ー–]\s*"
_PLAYERS_RANGE_RE = re.compile(r"(\d{1,2})" + _DASH + r"(\d{1,2})\s*人")
_PLAYERS_MIN_RE = re.compile(r"(\d{1,2})\s*人以上")
_PLAYERS_ONE_RE = re.compile(r"(\d{1,2})\s*人(?:用|で遊)")
_PLAYERS_ANY_RE = re.compile(r"何人でも")
_PLAYERS_HINT_RE = re.compile(r"人数|人用|プレイ|遊べ|何人")
_TIME_RE = re.compile(r"(?:プレイ時間|所要時間|ゲーム時間|遊ぶ時間|1ゲーム)[^0-9\n]{0,6}(\d{1,3})(?:" + _DASH
                      + r"(\d{1,3}))?\s*分")


def _spec_strings(raw: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            out.append(unicodedata.normalize("NFKC", o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(raw.get("technical_specs"))
    return out


def players_of(raw: dict[str, Any]) -> dict[str, Any] | None:
    """{"min": 2, "max": 6 (無ければ None), "label": "2〜6人"}。書いていなければ None。"""
    for s in _spec_strings(raw):
        if not _PLAYERS_HINT_RE.search(s):
            continue
        if _PLAYERS_ANY_RE.search(s):
            return {"min": 1, "max": None, "label": "何人でも"}
        m = _PLAYERS_RANGE_RE.search(s)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 < lo <= hi <= 99:
                return {"min": lo, "max": hi, "label": f"{lo}〜{hi}人" if lo != hi else f"{lo}人"}
        m = _PLAYERS_MIN_RE.search(s)
        if m and int(m.group(1)) > 0:
            return {"min": int(m.group(1)), "max": None, "label": f"{int(m.group(1))}人以上"}
        m = _PLAYERS_ONE_RE.search(s)
        if m and int(m.group(1)) > 0:
            n = int(m.group(1))
            return {"min": n, "max": n, "label": f"{n}人"}
    return None


def play_time_of(raw: dict[str, Any]) -> str | None:
    """「約10分」「15〜30分」。書いていなければ None。"""
    for s in _spec_strings(raw):
        m = _TIME_RE.search(s)
        if m:
            lo = int(m.group(1))
            if m.group(2):
                return f"{lo}〜{int(m.group(2))}分"
            return f"約{lo}分"
    return None


def many_players(p: dict[str, Any] | None) -> bool:
    """5 人以上で遊べるか (上限なし・「何人でも」を含む)。"""
    if not p:
        return False
    return p["max"] is None or p["max"] >= 5


# ---------------------------------------------------------------------------
# 選び方
# ---------------------------------------------------------------------------


def band_of(mm: int | None) -> dict[str, Any] | None:
    if mm is None:
        return None
    return next((b for b in BANDS if b["min"] <= mm < b["max"]), None)


def game_score(rec, mm: int, band: dict[str, Any]) -> float:
    """年齢の合い方 (その年齢から 20 / 1 歳下から 14 / それより下 6) + 知育スコア×0.45。

    クリスマスページ (xmas_score) から予算の点を外したもの。かるたは 1,000 円未満が多く、
    「上限の 3 割以上」の点を入れると安いだけで沈むため。
    """
    lo = band["lo"]
    score = 20.0 if mm >= lo else 14.0 if mm >= lo - 12 else 6.0
    return score + (rec.ivs_100 or 0) * 0.45


def select_band(records, raw_index: dict[str, dict[str, Any]], mm_index: dict[str, int],
                brand_index: dict[str, str] | None, band: dict[str, Any], *, min_ivs: float,
                top_n: int = TOP_N):
    """年齢帯の候補を top_n 件。並びは点の順。

    - 同じ商品の別出品は 1 件、同じメーカーは MAX_PER_BRAND 件まで (届かないときだけ戻して埋める)
    - 種類が偏らないように、まず種類ごとにいちばん点の高い 1 件を入れ、残りは点の順。1 つの種類は
      KIND_CAP 件まで。知育スコアだけで選ぶと、11-26 の実データではかるた 1・すごろく 0 になり、
      お正月の節と「種類」の絞り込みが空になった
    """
    from build_category_hubs import _name_key, _same_product  # 循環 import を避けて呼ぶときに読む
    brand_index = brand_index or {}
    scored = []
    for rec in records:
        if rec.ivs_score is None or rec.ivs_score < min_ivs:
            continue
        price = rec.best_price
        if not isinstance(price, (int, float)) or price <= 0 or price > BUDGET_MAX:
            continue
        mm = mm_index.get(rec.asin)
        if band_of(mm) is not band:
            continue
        kind = kind_of(raw_index.get(rec.asin) or {})
        if kind is None:
            continue
        scored.append((game_score(rec, mm, band), rec, kind.key))
    scored.sort(key=lambda x: (-x[0], -(x[1].ivs_100 or 0), x[1].best_price or 10**9, x[1].asin))
    order = {rec.asin: i for i, (_, rec, _) in enumerate(scored)}

    picked: list = []
    seen: list[str] = []
    used: dict[str, int] = {}
    per_kind: dict[str, int] = {}
    taken: set[str] = set()

    def take(rec, kind: str, *, brand_cap: bool) -> bool:
        key = _name_key(rec.name)
        if rec.asin in taken or _same_product(key, seen):
            return False
        brand = brand_index.get(rec.asin)
        if brand_cap and brand is not None and used.get(brand, 0) >= MAX_PER_BRAND:
            return False
        seen.append(key)
        taken.add(rec.asin)
        if brand is not None:
            used[brand] = used.get(brand, 0) + 1
        per_kind[kind] = per_kind.get(kind, 0) + 1
        picked.append(rec)
        return True

    for k in GAME_KINDS:  # 1. 種類ごとに 1 件
        for _, rec, kind in scored:
            if len(picked) >= top_n:
                break
            if kind == k.key and take(rec, kind, brand_cap=True):
                break
    for _, rec, kind in scored:  # 2. 残りは点の順 (種類ごとの上限あり)
        if len(picked) >= top_n:
            break
        if per_kind.get(kind, 0) < KIND_CAP:
            take(rec, kind, brand_cap=True)
    for cap in (True, False):  # 3. 足りなければ種類の上限、それでも足りなければメーカーの上限を外す
        for _, rec, kind in scored:
            if len(picked) >= top_n:
                break
            if rec.asin not in taken and not _same_product(_name_key(rec.name), seen):
                take(rec, kind, brand_cap=cap)
    picked.sort(key=lambda r: order[r.asin])
    return picked


# ---------------------------------------------------------------------------
# お正月遊び一覧 (12/1〜1/15 は上に出す)
# ---------------------------------------------------------------------------

# 遊び方は一般的な説明だけを書く。由来・歴史は公的な出典が無いので書かない (#216 の 4 章 6)。
# match は、このページの商品の名前・タグ (NFKC・小文字) に当てる正規表現。当たった商品を記事へつなぐ。
OSHOGATSU_PLAYS: tuple[dict[str, str], ...] = (
    {"key": "fukuwarai", "name": "福笑い", "where": "家の中",
     "how": "目かくしをして、顔の輪郭の上に目・鼻・口の部品を置き、できた顔をみんなで見て笑います。",
     "match": r"福笑い|ふくわらい"},
    {"key": "karuta", "name": "かるた", "where": "家の中",
     "how": "読み手が読み札を読み、それに合う絵札を早く取った人が札をもらいます。",
     "match": r"カルタ|かるた"},
    {"key": "sugoroku", "name": "すごろく", "where": "家の中",
     "how": "さいころを振ってコマを進め、早くゴールに着いた人の勝ちです。",
     "match": r"すごろく(?!や)|双六|スゴロク"},
    {"key": "bozu", "name": "坊主めくり", "where": "家の中",
     "how": "百人一首の絵札を裏返して積み、順にめくります。坊主の札が出たら、持っている札を全部出します。",
     "match": r"百人一首"},
    {"key": "koma", "name": "こま回し", "where": "家の中（床を傷つけない場所）・外",
     "how": "ひもを巻いて投げたり、指でひねったりして回し、長く回した人の勝ちです。",
     "match": r"独楽|コマ回し|こま回し|糸引きこま|逆立ちこま"},
    {"key": "kendama", "name": "けん玉", "where": "家の中",
     "how": "玉を皿にのせたり、けん先に入れたりする技に挑戦します。",
     "match": r"けん玉|剣玉|ケンダマ|けんだま"},
    {"key": "daruma", "name": "だるま落とし", "where": "家の中",
     "how": "積んだ胴を木づちで横から打ち抜き、だるまの頭を落とさずに残します。",
     "match": r"だるま落とし|ダルマ落とし"},
    {"key": "hanetsuki", "name": "羽根つき", "where": "外（広い場所）",
     "how": "羽子板で羽根を打ち合い、落とさずに続けます。",
     "match": r"羽根つき"},
    {"key": "takoage", "name": "凧揚げ", "where": "外（電線のない広い場所）",
     "how": "風を受けて凧を高く揚げます。",
     "match": r"凧"},
)


def build_oshogatsu(records, raw_index: dict[str, dict[str, Any]], mm_index: dict[str, int],
                    per_item: int = 3) -> list[dict[str, Any]]:
    """お正月遊びの一覧。当たる商品の記事 (このページに載っていない物も含む) を、知育スコアの高い順に最大 per_item 件つなぐ。

    対象は家族で遊べるゲームと同じ条件 (ゲームの種類が付く・対象年齢 13 歳未満・価格 1 万円まで)。
    知育スコアの下限は設けない (福笑いのような小さな物は点が低く、下限を置くと一覧が空になる)。
    """
    pool = []
    for rec in records:
        raw = raw_index.get(rec.asin) or {}
        mm = mm_index.get(rec.asin)
        price = rec.best_price
        if kind_of(raw) is None or (mm is not None and mm >= MAX_AGE_MONTHS):
            continue
        if not isinstance(price, (int, float)) or price <= 0 or price > BUDGET_MAX:
            continue
        pool.append((rec, _norm(rec.name), _text(raw)))
    pool.sort(key=lambda x: (-(x[0].ivs_100 or 0), x[0].asin))
    from build_category_hubs import _name_key, _same_product  # 循環 import を避けて呼ぶときに読む
    out = []
    for play in OSHOGATSU_PLAYS:
        pat = re.compile(play["match"])
        # 商品名に当たる物を先に、タグだけで当たる物 (顔シールの本に「福笑い」のタグ等) は後に
        ordered = ([rec for rec, name, _ in pool if pat.search(name)]
                   + [rec for rec, name, text in pool if not pat.search(name) and pat.search(text)])
        hits, seen = [], []
        for rec in ordered:  # 同じ商品の別出品・色違いは 1 件にする
            key = _name_key(rec.name)
            if _same_product(key, seen):
                continue
            seen.append(key)
            hits.append({"name": rec.name, "url": f"/products/{rec.asin.lower()}/"})
            if len(hits) >= per_item:
                break
        out.append({k: play[k] for k in ("key", "name", "where", "how")} | {"items": hits})
    return out


# ---------------------------------------------------------------------------
# よくある質問 (答えはこのページの数字か公的な情報で出せるものだけ)
# ---------------------------------------------------------------------------


def _yen(v: float | int) -> str:
    return f"{int(round(v)):,}円"


def _age_years(mm: int) -> str:
    """表・カードの対象年齢 (hugo/layouts/partials/age_label.html) と同じ書き方から「〜」を除いたもの。"""
    if mm < 12:
        return f"{mm}ヶ月"
    if mm % 12 == 0:
        return f"{mm // 12}歳"
    if mm % 12 == 6:
        return f"{mm // 12}.5歳"
    return f"{mm / 12:.1f}歳"


def band_age_phrase(band: dict[str, Any]):
    """順位の理由の年齢の文。年齢帯の幅 (4〜5歳) を言う。クリスマスページの言い方 (「5歳以上向けで4歳にぴったり」) は
    1 つの年齢のページ向けで、帯のページでは食い違うため。"""
    def phrase(mm: int | None, page: dict[str, Any]) -> str | None:
        if mm is None:
            return None
        if mm >= band["lo"]:
            return f"{_age_years(mm)}から遊べて{band['label']}に合う"
        return f"{_age_years(mm)}から遊べて、{band['label']}でも長く使える"
    return phrase


def _kind_key(e: dict[str, Any]) -> str | None:
    return ((e.get("game") or {}).get("kind") or {}).get("key")


def build_faq(entries: list[dict[str, Any]], *, stockout: int, when: str) -> list[dict[str, Any]]:
    faq: list[dict[str, Any]] = []
    n = len(entries)

    karuta = [e for e in entries if _kind_key(e) == "karuta"
              and e.get("age_min_months") is not None]
    if len(karuta) >= 2:
        ages = sorted(int(e["age_min_months"]) for e in karuta)
        mode = max(set(ages), key=lambda a: (ages.count(a), -a))
        if ages[0] == ages[-1]:
            head = f"このページのかるた{len(karuta)}件は、どれも対象年齢が{_age_years(ages[0])}からです。"
        else:
            head = (f"このページのかるた{len(karuta)}件の対象年齢は、いちばん下が{_age_years(ages[0])}から、"
                    f"多いのは{_age_years(mode)}からです。")
        faq.append({"q": "かるたは何歳から遊べる？", "a": head + "対象年齢は各商品のカードに書いています。"})

    young = [e for e in entries if e["band"] == "age4"
             and _kind_key(e) in ("board", "card", "sugoroku")
             and e.get("age_min_months") is not None and int(e["age_min_months"]) <= 48]
    if young:
        top = young[:3]
        faq.append({
            "q": "4歳でも遊べるボードゲームは？",
            "a": (f"このページで対象年齢が4歳からのボードゲーム・カードゲーム・すごろくは{len(young)}件です。"
                  f"上位は{'・'.join(e['name'] for e in top)}です。"),
            "links": [{"label": e["name"], "url": e["url_internal"]} for e in top],
        })

    many = [e for e in entries if many_players((e.get("game") or {}).get("players"))]
    known = sum(1 for e in entries if (e.get("game") or {}).get("players"))
    if many:
        top = many[:3]
        faq.append({
            "q": "大人数（5人以上）で遊べるものは？",
            "a": (f"人数が分かる{known}件のうち、5人以上で遊べるのは{len(many)}件です。"
                  f"たとえば{'・'.join(e['name'] for e in top)}です。人数は比較表に書いています（分かるものだけ）。"),
            "links": [{"label": e["name"], "url": e["url_internal"]} for e in top],
        })

    prices = [int(e["best_price"]) for e in entries if isinstance(e.get("best_price"), (int, float))
              and e["best_price"] > 0]
    if prices:
        u3000 = sum(1 for p in prices if p <= 3000)
        u1000 = sum(1 for p in prices if p <= 1000)
        med = int(round(statistics.median(prices) / 10.0) * 10)
        faq.append({
            "q": "お年玉で買える値段のものは？",
            "a": (f"このページの{n}件のうち、最安値が3,000円以下は{u3000}件、1,000円以下は{u1000}件です。"
                  f"真ん中の値は約{_yen(med)}です。上の「予算」で絞り込めます。"),
        })

    faq.append({
        "q": "いま品切れの心配はありますか？",
        "a": (f"{when}このページの{n}件のうち、直近30日に Amazon で在庫切れを観測したのは{stockout}件です。"
              + ("該当する商品には「在庫切れを観測」の印を付けています。" if stockout else "")
              + "在庫と配送日はリンク先で必ずご確認ください。"),
    })
    faq.append({
        "q": "下のきょうだいがいても大丈夫ですか？",
        "a": ("ボードゲームのコマやさいころ、カードは小さな部品です。トイレットペーパーの芯（直径約39mm）を通る大きさの物は、"
              "3歳未満の子どもが口に入れて窒息するおそれがあります（政府広報オンライン）。"
              "遊んだあとは部品を数えて、手の届かない場所に片付けてください。"),
        "links": [{"label": "政府広報オンライン（誤飲・窒息事故）",
                   "url": "https://www.gov-online.go.jp/article/202408/entry-6450.html"}],
    })
    return faq


# ---------------------------------------------------------------------------
# ページ全体
# ---------------------------------------------------------------------------


def build_payload(records, raw_index: dict[str, dict[str, Any]], mm_index: dict[str, int],
                  brand_index: dict[str, str] | None, *, min_ivs: float, generated_at: str,
                  rank_days_threshold: int, prioritize=None, serialize=None,
                  extras_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """family-games.json の中身。年齢帯が 1 つも MIN_ITEMS に届かなければ items は空 (noindex)。

    prioritize: 楽天ランキングの売れ筋を前に出す関数 (build_category_hubs.prioritize_ranked を部分適用したもの)
    serialize: rec, rank → dict (build_feature_lists._record_to_payload_common)
    """
    extras_kwargs = extras_kwargs or {}
    entries: list[dict[str, Any]] = []
    bands_out: list[dict[str, Any]] = []
    stockout = 0
    fetched: list[str] = []
    for band in BANDS:
        items = select_band(records, raw_index, mm_index, brand_index, band, min_ivs=min_ivs)
        if len(items) < MIN_ITEMS:
            bands_out.append({"key": band["key"], "label": band["label"], "count": 0,
                              "skipped": f"too_few: {len(items)} < {MIN_ITEMS}"})
            continue
        if prioritize is not None:
            items = prioritize(items)
        page = {"label": band["phrase"], "lo": band["lo"], "hi": band["max"] - 1}
        per, extra = xmas_page.build_page_extras(
            items, raw_index, page, mm_index, rank_days_threshold=rank_days_threshold,
            generated_at=generated_at, type_of=kind_of, scope="この年齢",
            age_phrase=band_age_phrase(band), **extras_kwargs)
        stockout += extra["stockout_count"]
        if extra.get("price_fetched_at"):
            fetched.append(extra["price_fetched_at"])
        for idx, rec in enumerate(items, start=1):
            entry = serialize(rec, idx)
            if entry.get("age_min_months") is None:
                # 表とカードの年齢は記事の表示と同じ値 (rec.age_min_months)。無ければ帯を決めた値
                entry["age_min_months"] = mm_index.get(rec.asin)
            if getattr(rec, "rank_days_30", 0) > 0:
                entry["rank_days_30"] = rec.rank_days_30
                entry["best_rank_30"] = rec.best_rank_30
            raw = raw_index.get(rec.asin) or {}
            x = per.get(rec.asin) or {}
            x["players"] = (players_of(raw) or {}).get("label")
            x["play_time"] = play_time_of(raw)
            entry["xmas"] = x
            kind = x.get("play_type")
            entry["game"] = {"kind": kind, "players": players_of(raw)}
            entry["band"] = band["key"]
            entry["band_label"] = band["label"]
            entries.append(entry)
        bands_out.append({"key": band["key"], "label": band["label"], "count": len(items),
                          "picks": extra["picks"], "type_counts": extra["type_counts"],
                          "median_price": extra["median_price"], "price_min": extra["price_min"],
                          "price_max": extra["price_max"], "dev_stage_key": extra["dev_stage_key"]})

    kinds_seen = {(e["game"]["kind"] or {}).get("key") for e in entries}
    kinds = [{"key": k.key, "emoji": k.emoji, "label": k.label,
              "count": sum(1 for e in entries if (e["game"]["kind"] or {}).get("key") == k.key)}
             for k in GAME_KINDS if k.key in kinds_seen]
    parsed = [f for f in fetched if xmas_page._parse_ts(f) is not None]
    price_fetched_at = max(parsed, key=xmas_page._parse_ts) if parsed else generated_at
    when = ""
    dt = xmas_page._parse_ts(price_fetched_at)
    if dt:
        j = dt.astimezone(xmas_page._JST)
        when = f"{j.month}月{j.day}日{j.hour}時の価格の記録で、"
    return {
        "generated_at": generated_at,
        "type": PAGE_KEY,
        "label": "家族で遊べるゲーム",
        "count": len(entries),
        "items": entries,
        "budget_max": BUDGET_MAX,
        "games_page": {
            "bands": bands_out,
            "kinds": kinds,
            "budgets": [{"key": k, "label": label, "max": mx} for k, label, mx in BUDGETS],
            "players_known": sum(1 for e in entries if e["game"]["players"]),
            "price_fetched_at": price_fetched_at,
            "stockout_count": stockout,
            "oshogatsu": build_oshogatsu(records, raw_index, mm_index),
            "faq": build_faq(entries, stockout=stockout, when=when),
        },
    }
