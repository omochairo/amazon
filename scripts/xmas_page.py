"""クリスマス × 年齢ページの付加情報 (navi-brain#214 v2 第 1 弾)。

build_category_hubs.py がクリスマスページの商品を選んだあと、ここで 1 件ごとと
ページ全体の付加情報を作り、feature JSON の ``items[].xmas`` と ``xmas_page`` に足す。
テンプレートは hugo/layouts/partials/xmas_page.html と xmas_card.html。

作るもの (要件は navi-brain#214 の 4 章):
  1 件ごと  … 遊びのタイプ・順位の理由・購入前に確認・良い声 2 / 気になる声 1・
              メーカー公式で裏付けた事実 1 つ・ふだんの価格との差・直近 30 日の在庫切れ・3 店の価格
  ページ    … 結論 (多い遊びのタイプ・価格の中央値)・迷ったらこの 3 つ・タイプの内訳・
              在庫切れの件数・年齢帯ごとのよくある質問

方針:
  - 文章は作らない。記事 JSON にある声・主張と、数えた数字だけを並べる
  - 記事に無ければ出さない (空欄を埋めるための言い換えをしない)
  - 遊びのタイプは診断 (hugo/assets/js/diagnosis_core.js の INTERESTS) をそのまま読む。
    2 か所に語を持つと片方だけ直る事故が起きるので、JS を正本にしてビルド時に読む
"""
from __future__ import annotations

import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from age_semantics import months_label  # noqa: E402
from build_price_dashboard import evaluate_drop, load_merged_history  # noqa: E402
import market_prices  # noqa: E402
import price_overlay  # noqa: E402
from price_spark import load_out_of_stock_points  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIAGNOSIS_JS = ROOT / "hugo" / "assets" / "js" / "diagnosis_core.js"
PRICE_WATCH_DIR = ROOT / "data" / "price_watch"
PRICE_HISTORY_DIR = ROOT / "data" / "price_history"

STOCKOUT_DAYS = 30
GOOD_VOICES = 2
# 楽天ランキングの優先 (build_category_hubs.RANK_DAYS_PRIORITY_THRESHOLD) と同じ値。循環 import を避けて引数で受ける
_JST = timezone(timedelta(hours=9))

# 発達の目安 (hugo/data/development_stages.json) のどのキーを引くか
# 0 歳のページは月齢の幅が大きいので、贈る時期に多い 6 か月ごろを引く
_DEV_STAGE_KEYS = ((72, "72m"), (60, "60m"), (48, "48m"), (36, "36m"), (24, "24m"), (12, "12m"), (0, "6m"))

# ---------------------------------------------------------------------------
# 遊びのタイプ (診断の INTERESTS)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayType:
    key: str
    emoji: str
    label: str
    kw: tuple[str, ...]


_INTEREST_RE = re.compile(
    r'\{\s*value:\s*"(\w+)",\s*emoji:\s*"([^"]+)",\s*label:\s*"([^"]+)".*?kw:\s*\[(.*?)\]', re.S)


@lru_cache(maxsize=None)
def load_play_types(path: str | None = None) -> tuple[PlayType, ...]:
    """diagnosis_core.js の INTERESTS を読む (語の無い「おまかせ」は除く)。"""
    js = Path(path or DIAGNOSIS_JS).read_text(encoding="utf-8")
    start = js.index("var INTERESTS = [")
    block = js[start: js.index("];", start)]
    out = []
    for m in _INTEREST_RE.finditer(block):
        kws = tuple(k.lower() for k in re.findall(r'"([^"]+)"', m.group(4)))
        if kws:
            out.append(PlayType(m.group(1), m.group(2), m.group(3), kws))
    return tuple(out)


def play_type_of(raw: dict[str, Any], types: tuple[PlayType, ...] | None = None) -> PlayType | None:
    """診断の genreOf と同じ。INTERESTS の順に、タグか商品名に語が入っている最初のタイプ。"""
    types = types if types is not None else load_play_types()
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    tags = [str(t).lower() for t in (raw.get("tags") or []) if isinstance(t, str)]
    name = str(product.get("name") or "").lower()
    for t in types:
        for kw in t.kw:
            if kw in name or any(kw in tag for tag in tags):
                return t
    return None


def _type_dict(t: PlayType | None) -> dict[str, str] | None:
    return {"key": t.key, "emoji": t.emoji, "label": t.label} if t else None


# ---------------------------------------------------------------------------
# 記事からの抜き出し (声・公式・向かない人・店)
# ---------------------------------------------------------------------------

_BUYER = ("購入者", "ユーザー", "利用者", "口コミ", "レビュー投稿")
_MEDIA = ("第三者", "ブログ", "youtube", "メディア", "ライター", "レビュアー", "価格.com", "比較", "専門家")
_SHOPS = ("amazon.", "rakuten.", "yahoo.", "valuecommerce.", "a8.net", "afl.", "youtube.", "youtu.be")
_OFFICIAL_CATEGORIES = ("safety", "quality", "durability")  # 仕様・安全・品質。比較の優位 (宣伝になりやすい) は出さない
_HYPE = ("絶対", "必ず", "最強", "No.1", "ナンバーワン", "間違いな", "100%", "誰でも", "日本一", "世界一")
_PRICE_RE = re.compile(r"[0-9０-９,]+\s*円|最安|値下がり|値上がり|セール|特価|割引")


def _sources(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(s.get("id")): s for s in raw.get("sources") or [] if isinstance(s, dict) and s.get("id")}


def _source_kind(author: Any) -> str:
    a = str(author or "").lower()
    if "公式" in a:
        return "official"
    if any(w.lower() in a for w in _BUYER):
        return "buyer"
    if any(w.lower() in a for w in _MEDIA):
        return "media"
    return "other"


_KIND_LABEL = {"buyer": "購入者の声", "media": "レビュー記事・動画", "official": "メーカー公式", "other": "販売ページなど"}


def _source_note(ids: list[Any], src: dict[str, dict[str, Any]]) -> str:
    """「購入者の声 2・レビュー記事・動画 1」。出典が引けなければ空。"""
    kinds = Counter(_source_kind(src[str(i)].get("author")) for i in ids if str(i) in src)
    order = ("buyer", "media", "official", "other")
    return "・".join(f"{_KIND_LABEL[k]} {kinds[k]}" for k in order if kinds.get(k))


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def voices_of(raw: dict[str, Any]) -> dict[str, Any] | None:
    """良い声 2・気になる声 1 (review_signals の high_points / concerns の先頭) と出典の数。"""
    rs = raw.get("review_signals")
    if not isinstance(rs, dict):
        return None
    src = _sources(raw)

    def entries(values: Any) -> list[dict[str, str]]:
        """購入者・レビュー記事の出典があるものを先に (メーカーの説明だけの「良い点」を声として先に出さない)。"""
        out = []
        for pos, v in enumerate(values or []):
            if not isinstance(v, dict) or not _clean(v.get("text")):
                continue
            ids = v.get("supporting_source_ids") or []
            voiced = any(str(i) in src and _source_kind(src[str(i)].get("author")) in ("buyer", "media")
                         for i in ids)
            out.append((0 if voiced else 1, pos, {"text": _clean(v.get("text")),
                                                  "sources": _source_note(ids, src)}))
        return [e for _, _, e in sorted(out, key=lambda t: t[:2])]

    good = entries(rs.get("high_points"))[:GOOD_VOICES]
    concern = next(iter(entries(rs.get("concerns"))), None)
    if not good and not concern:
        return None
    return {"good": good, "concern": concern}


def voice_source_count(raw: dict[str, Any]) -> int:
    """声の厚さ: 声 (良い点・気になる点・使う場面・層ごとの声) を支える出典の数 (メーカー公式・販売ページは数えない)。"""
    rs = raw.get("review_signals")
    if not isinstance(rs, dict):
        return 0
    src = _sources(raw)
    ids: set[str] = set()
    for key in ("high_points", "concerns", "use_scenes", "segment_voices"):
        for v in rs.get(key) or []:
            if isinstance(v, dict):
                ids.update(str(i) for i in v.get("supporting_source_ids") or [])
    return sum(1 for i in ids if i in src and _source_kind(src[i].get("author")) in ("buyer", "media"))


def _is_shop_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return not host or any(s in host for s in _SHOPS)


def official_fact(raw: dict[str, Any]) -> dict[str, Any] | None:
    """メーカー公式の出典で裏付けた主張 1 つ (安全 > 品質 > 耐久。複数の出典で確かめたものを先に)。

    リンクはメーカー自身のサイトのときだけ付ける。メーカー公式の多くは Amazon の商品説明なので、
    そのときは「メーカーの商品説明より」として出し、リンクは付けない (店のボタンと重ねない)。
    """
    src = _sources(raw)
    best = None
    for pos, c in enumerate(raw.get("claims") or []):
        if not isinstance(c, dict) or c.get("category") not in _OFFICIAL_CATEGORIES:
            continue
        text = _clean(c.get("claim"))
        if not text or any(w in text for w in _HYPE) or _PRICE_RE.search(text):
            continue
        official = [src[str(i)] for i in c.get("supporting_source_ids") or []
                    if str(i) in src and _source_kind(src[str(i)].get("author")) == "official"]
        if not official:
            continue
        maker = next((s for s in official if str(s.get("url") or "").startswith("https://")
                      and not _is_shop_url(str(s.get("url")))), None)
        key = (_OFFICIAL_CATEGORIES.index(c["category"]), 0 if c.get("cross_checked") else 1,
               0 if maker else 1, pos)
        if best is None or key < best[0]:
            best = (key, text, c["category"], maker)
    if best is None:
        return None
    _, text, category, maker = best
    out: dict[str, Any] = {"text": text, "category": category}
    if maker:
        out["url"] = str(maker["url"])
        out["site"] = (urlparse(out["url"]).hostname or "").removeprefix("www.")
    return out


def not_for_of(raw: dict[str, Any]) -> str | None:
    pf = raw.get("persona_fit")
    if not isinstance(pf, dict):
        return None
    return next((_clean(s) for s in pf.get("not_recommended_for") or [] if _clean(s)), None)


def fits_of(raw: dict[str, Any], limit: int = 2) -> list[str]:
    """こんな子に向く (persona_fit.recommended_for の先頭 limit 件)。"""
    pf = raw.get("persona_fit")
    if not isinstance(pf, dict):
        return []
    return [_clean(s) for s in pf.get("recommended_for") or [] if _clean(s)][:limit]


def scene_of(raw: dict[str, Any]) -> str | None:
    """使う場面 (review_signals.use_scenes の 1 件目)。"""
    rs = raw.get("review_signals")
    if not isinstance(rs, dict):
        return None
    for v in rs.get("use_scenes") or []:
        text = _clean(v.get("text")) if isinstance(v, dict) else _clean(v)
        if text:
            return text
    return None


def shop_urls(rec, raw: dict[str, Any],
              matched: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict[str, str]:
    """楽天・Yahoo の商品 URL。表示する価格 (rec.price_rakuten / price_yahoo) と同じ出どころにする。

    価格は build_feature_lists.overlay_current_prices が日次の照合結果
    (data/raw/{rakuten,yahoo}_matched.json) で上書きしているので、URL も同じ規則で選ぶ:
      - 価格が無い (0・None。照合に落ちて外れ値として捨てた場合を含む) 店は出さない
      - 照合結果が品質の検査 (market_prices.matched_passes_quality) を通れば、その URL
      - 通らなければ記事 JSON の URL。ただし別の ASIN の出品ページなら出さない (#9244)
    Amazon は表示側で amazon_affiliate_url.html がタグを付けて作る。
    """
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
    amazon_price = rec.price_amazon or 0
    title = rec.name_full or rec.name or ""
    out = {}
    for shop in ("rakuten", "yahoo"):
        price = getattr(rec, f"price_{shop}", None)
        if not isinstance(price, int) or price <= 0:
            continue
        m = (matched or {}).get(shop, {}).get(rec.asin)
        if m and market_prices.matched_passes_quality(m, amazon_price, amazon_title=title):
            url = m.get("url")
        else:
            block = prices.get(shop) if isinstance(prices.get(shop), dict) else {}
            url = block.get("url")
            if isinstance(url, str) and market_prices.other_asin_in_url(url, rec.asin):
                url = None
        if isinstance(url, str) and url.startswith("https://"):
            out[shop] = url
    return out


# ---------------------------------------------------------------------------
# 価格 (ふだんの価格との差・在庫切れ)
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def price_signal(rec, *, now: datetime, price_watch_dir: Path = PRICE_WATCH_DIR,
                 price_history_dir: Path = PRICE_HISTORY_DIR) -> dict[str, Any] | None:
    """Amazon の価格が「ふだんの価格」(/price/ と同じ保持日数の中央値) より安いか。

    値下げ速報と同じ evaluate_drop を使うので、5% 未満・100 円未満の差や、観測が 7 日に
    満たない商品は出さない。all_time_low は記録のある全期間での最安。
    """
    current = rec.price_amazon
    if not isinstance(current, int) or isinstance(current, bool) or current <= 0:
        return None
    # 日次の観測が無い (記事を書いた時の価格のまま) か、在庫切れと観測した商品は比べない
    ts = _parse_ts(rec.fetched_at)
    if ts is None or price_overlay.is_explicitly_unavailable(getattr(rec, "availability", None)):
        return None
    history = load_merged_history(rec.asin, price_watch_dir, price_history_dir)
    drop = evaluate_drop(current, ts, history)
    if not drop:
        return None
    return {"usual": drop["reference"], "drop_yen": drop["drop_yen"],
            "drop_pct": drop["drop_pct"], "all_time_low": bool(drop["all_time_low"])}


def stocked_out_recently(asin: str, *, now: datetime, price_watch_dir: Path = PRICE_WATCH_DIR,
                         price_history_dir: Path = PRICE_HISTORY_DIR, days: int = STOCKOUT_DAYS) -> bool:
    """直近 days 日に Amazon の在庫切れを観測したか (価格推移のグラフの点線と同じ記録)。"""
    since = now - timedelta(days=days)
    for root in (price_history_dir, price_watch_dir / "history"):
        for p in load_out_of_stock_points(root, asin):
            ts = _parse_ts(p.get("ts"))
            if ts is not None and ts >= since:
                return True
    return False


# ---------------------------------------------------------------------------
# 順位の理由
# ---------------------------------------------------------------------------


def _age_phrase(mm: int | None, page: dict[str, Any]) -> str | None:
    if mm is None:
        return None
    lo, label = page["lo"], page["label"]
    if mm == lo:
        return f"{label}ちょうどから遊べる" if lo < 72 else f"{label}（6歳）から遊べる"
    if mm > lo:
        return f"{months_label(mm)}向けで{label}にぴったり"
    if mm >= lo - 12:
        return f"{months_label(mm).removesuffix('以上')}から遊べて、{label}でも長く使える"
    return f"{months_label(mm).removesuffix('以上')}から遊べる息の長いおもちゃ"


def rank_reasons(recs, page: dict[str, Any], mm_index: dict[str, int], types: dict[str, PlayType | None],
                 *, rank_days_threshold: int, scope: str = "このページ",
                 age_phrase=None) -> dict[str, list[str]]:
    """{asin: 順位の理由の短い文 (2〜3 個)}。並び (recs の順) を決めた要素を先に書く。

    並びの決め方 (build_category_hubs): 楽天ランキングに直近 30 日で一定日数入った商品を先に、
    残りは年齢の合い方・予算・知育スコアの点の順。遊びのタイプの中での順番は並びを決めた要素では
    ないが、読者がタイプで選ぶときの手がかりとして、同じ文が重なるときの 3 つ目に使う。
    1 ページで同じ組み合わせの文を出さない (重なるときはタイプ → 価格 → 両方の順に足す)。
    """
    values = [r.ivs_100 for r in recs if r.ivs_100 is not None]
    type_seen: Counter = Counter()
    type_total = Counter(t.key for t in types.values() if t)
    out: dict[str, list[str]] = {}
    used: set[tuple[str, ...]] = set()
    for rec in recs:
        base: list[str] = []
        days = getattr(rec, "rank_days_30", 0) or 0
        if days >= rank_days_threshold:
            base.append(f"楽天のおもちゃランキングに直近30日で{days}日入った売れ筋")
        age = (age_phrase or _age_phrase)(mm_index.get(rec.asin), page)
        if age:
            base.append(age)
        if rec.ivs_100 is not None:
            # 同点は同じ順位、その次は飛ばす (95・95・90 → 1・1・3 位)
            pos = 1 + sum(1 for v in values if v > rec.ivs_100)
            tie = "タイ" if values.count(rec.ivs_100) > 1 else ""
            # 「このページ◯位」だとランキングの順位と取り違えるので、スコアの順位と書く
            base.append(f"知育スコア{int(rec.ivs_100)}（スコア順で{pos}位{tie}）")
        extra: list[str] = []
        t = types.get(rec.asin)
        if t:
            type_seen[t.key] += 1
            extra.append(f"{scope}で唯一の「{t.label}」" if type_total[t.key] == 1
                         else f"「{t.label}」では{scope}{type_seen[t.key]}番目")
        if isinstance(rec.best_price, (int, float)) and rec.best_price > 0:
            extra.append(f"最安値{int(rec.best_price):,}円")
        candidates = [tuple(base[:3])]
        candidates += [tuple(base[:2] + [e]) for e in extra]
        candidates.append(tuple(base[:2] + extra))
        chosen = next((c for c in candidates if c and c not in used), candidates[-1])
        used.add(chosen)
        out[rec.asin] = list(chosen)
    return out


# ---------------------------------------------------------------------------
# ページ全体
# ---------------------------------------------------------------------------


def _band(page: dict[str, Any]) -> str:
    return "baby" if page["lo"] < 36 else "kids" if page["lo"] < 72 else "school"


def _dev_stage_key(lo: int) -> str:
    return next(k for m, k in _DEV_STAGE_KEYS if lo >= m)


def _yen(v: float | int) -> str:
    return f"{int(round(v)):,}円"


def _median_price(prices: list[int]) -> int | None:
    if not prices:
        return None
    return int(round(statistics.median(prices) / 10.0) * 10)


def build_faq(page: dict[str, Any], *, prices: list[int], stockout: int, n_items: int,
              younger_ok: int, characters: list[dict[str, Any]], type_counts: list[dict[str, Any]],
              price_fetched_at: str | None, older_min: int) -> list[dict[str, Any]]:
    """年齢帯ごとのよくある質問。答えはこのページの数字か公的な情報で出せるものだけ。"""
    label = page["label"]
    faq: list[dict[str, Any]] = []
    med = _median_price(prices)
    if med:
        faq.append({
            "q": f"{label}のクリスマスプレゼントの予算の目安は？",
            "a": (f"このページの{n_items}件の最安値は{_yen(min(prices))}〜{_yen(max(prices))}で、"
                  f"真ん中の値は約{_yen(med)}です。1万円までの商品から選んでいます。"
                  "3,000円以下の小さな贈り物と組み合わせる方法もあります。"),
        })
    when = ""
    dt = _parse_ts(price_fetched_at)
    if dt:
        j = dt.astimezone(_JST)
        when = f"{j.month}月{j.day}日{j.hour}時の価格の記録で、"
    faq.append({
        "q": "いま品切れの心配はありますか？",
        "a": (f"{when}このページの{n_items}件のうち、直近30日に Amazon で在庫切れを観測したのは{stockout}件です。"
              + ("該当する商品には「在庫切れを観測」の印を付けています。" if stockout else "")
              + "在庫と配送日はリンク先で必ずご確認ください。"),
    })
    band = _band(page)
    if band == "baby":
        faq.append({
            "q": "小さい部品の誤飲が心配です。何を確かめればいいですか？",
            "a": ("トイレットペーパーの芯（直径約39mm）を通る大きさの物は、3歳未満の子どもが口に入れて"
                  "窒息するおそれがあります（政府広報オンライン）。対象年齢の表示と、部品が外れないかを確かめてください。"
                  "このページは対象年齢が{0}以下の商品だけを選んでいます。").format(label),
            "links": [{"label": "政府広報オンライン（誤飲・窒息事故）",
                       "url": "https://www.gov-online.go.jp/article/202408/entry-6450.html"}],
        })
        faq.append({
            "q": "月齢の差が大きい時期ですが、どう選べばいいですか？",
            "a": ((f"このページには対象年齢が{label}より少し下の物も入れています（{younger_ok}件）。"
                   if younger_ok else f"このページの{n_items}件は、どれも対象年齢が{label}からの物です。")
                  + "いまできることに合った物を選ぶと、すぐに遊び始められます。"
                  "商品ごとの対象年齢はカードに書いています。"),
        })
    elif band == "kids":
        top = "と".join(f"「{t['label']}」（{t['count']}件）" for t in type_counts[:2])
        faq.append({
            "q": "男の子と女の子で選び方は変わりますか？",
            "a": ("遊びに男女の決まりはありません。このページは男女で分けず、遊びのタイプが偏らないように選んでいます。"
                  + (f"このページで多いのは{top}です。" if top else "")
                  + "普段よく手を伸ばす遊びのタイプから選ぶのがおすすめです。"),
        })
        faq.append({
            "q": "下のきょうだいと一緒に使えますか？",
            "a": ((f"このページの{n_items}件のうち、対象年齢が{label}より下から遊べる物は{younger_ok}件です。"
                   if younger_ok else f"このページの{n_items}件は、どれも対象年齢が{label}以上です。")
                  + "ただし小さな部品は、年下のきょうだいが口に入れると誤飲・窒息の原因になります（政府広報オンライン）。"
                  "3歳未満の子がいる家では、遊ぶ場所と片付け場所を分けてください。"),
        })
        if characters:
            names = "・".join(f"{c['label']}（{c['count']}件）" for c in characters)
            names = names + "が"
            faq.append({
                "q": "キャラクターのおもちゃでもいいですか？",
                "a": (f"好きなキャラクターは遊ぶきっかけになります。このページにはキャラクターの商品として{names}あります。"
                      "キャラクターごとのページでは、対象年齢と遊び方で選び分けています。"),
                "links": [{"label": f"{c['label']}のおもちゃ", "url": c["url"]} for c in characters],
            })
    else:
        faq.append({
            "q": "小学生には幼すぎませんか？",
            "a": ((f"このページの{n_items}件のうち、対象年齢の下限が8歳以上の物は{older_min}件です。"
                   "高学年に贈るなら、対象年齢の下限が高い物を選ぶと物足りなさを避けられます。"
                   if older_min else
                   f"このページの{n_items}件は、どれも対象年齢の下限が8歳未満で、低学年から遊べる物です。"
                   "高学年に贈るなら、商品の記事で遊びの難しさを確かめてください。")
                  + "対象年齢はカードに書いています。"),
        })
        think = next((t["count"] for t in type_counts if t["key"] == "think"), 0)
        if think:
            faq.append({
                "q": "親子で一緒に遊べるものはありますか？",
                "a": (f"このページでは「考える・ゲーム」のタイプが{think}件です。"
                      "ボードゲームやパズルは冬休みに家族や祖父母と一緒に遊べます。"),
            })
    return faq


def build_page_extras(recs, raw_index: dict[str, dict[str, Any]], page: dict[str, Any],
                      mm_index: dict[str, int], *, rank_days_threshold: int, now: datetime | None = None,
                      price_watch_dir: Path = PRICE_WATCH_DIR, price_history_dir: Path = PRICE_HISTORY_DIR,
                      generated_at: str | None = None,
                      characters_of=None,
                      matched: dict[str, dict[str, dict[str, Any]]] | None = None,
                      type_of=None, scope: str = "このページ", age_phrase=None,
                      ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """(asin ごとの付加情報, ページの付加情報) を返す。recs は表示の順。

    characters_of: raw → [(label, url)] (キャラクター別ページのどれに当たるか)。テストでは省ける。
    matched: {"rakuten": {asin: 照合結果}, "yahoo": {...}} (楽天・Yahoo の URL を価格と揃える)。
    type_of: raw → PlayType | None。省くと診断の遊びのタイプ (play_type_of)。家族で遊べるゲームの
    ページ (family_games_page.py) はゲームの種類を渡す。並び順は渡したタイプの出てきた順ではなく件数順。
    scope / age_phrase: 順位の理由の「このページで唯一の…」の範囲の言い方と、年齢の文を作る関数 (mm, page → str)。
    1 ページに年齢帯が複数あるページ用。省くとクリスマスページと同じ。
    """
    now = now or datetime.now(timezone.utc)
    type_of = type_of or play_type_of
    types = {r.asin: type_of(raw_index.get(r.asin) or {}) for r in recs}
    reasons = rank_reasons(recs, page, mm_index, types, rank_days_threshold=rank_days_threshold,
                           scope=scope, age_phrase=age_phrase)
    per: dict[str, dict[str, Any]] = {}
    fetched = []
    for rec in recs:
        raw = raw_index.get(rec.asin) or {}
        price = price_signal(rec, now=now, price_watch_dir=price_watch_dir, price_history_dir=price_history_dir)
        per[rec.asin] = {
            "play_type": _type_dict(types[rec.asin]),
            "rank_reasons": reasons.get(rec.asin, []),
            "not_for": not_for_of(raw),
            "fits": _drop_reason_dup(fits_of(raw), rec.reason),
            "scene": scene_of(raw),
            "voices": voices_of(raw),
            "voice_sources": voice_source_count(raw),
            "official": official_fact(raw),
            "price": price,
            "stockout_30d": stocked_out_recently(rec.asin, now=now, price_watch_dir=price_watch_dir,
                                                 price_history_dir=price_history_dir),
            "shop_urls": shop_urls(rec, raw, matched),
            "prices": {k: v for k, v in (("amazon", rec.price_amazon), ("rakuten", rec.price_rakuten),
                                         ("yahoo", rec.price_yahoo)) if isinstance(v, int) and v > 0},
        }
        if rec.fetched_at:
            fetched.append(rec.fetched_at)

    order = {t.key: i for i, t in enumerate(load_play_types())}
    counts = Counter(t.key for t in types.values() if t)
    by_key = {t.key: t for t in types.values() if t}
    type_counts = [{**_type_dict(by_key[k]), "count": n}
                   for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))]
    untyped = sum(1 for t in types.values() if t is None)

    prices = [int(r.best_price) for r in recs if isinstance(r.best_price, (int, float)) and r.best_price > 0]
    stockout = sum(1 for e in per.values() if e["stockout_30d"])
    younger_ok = sum(1 for r in recs if (mm_index.get(r.asin) is not None and mm_index[r.asin] < page["lo"]))
    older_min = sum(1 for r in recs if (mm_index.get(r.asin) or 0) >= 96)

    characters: list[dict[str, Any]] = []
    if characters_of:
        tally: dict[tuple[str, str], int] = {}
        for rec in recs:
            for label, url in characters_of(raw_index.get(rec.asin) or {}):
                tally[(label, url)] = tally.get((label, url), 0) + 1
        characters = [{"label": l, "url": u, "count": n}
                      for (l, u), n in sorted(tally.items(), key=lambda kv: -kv[1])]

    parsed = [f for f in fetched if _parse_ts(f) is not None]
    price_fetched_at = max(parsed, key=_parse_ts) if parsed else generated_at
    picks = _top3(recs, per)
    page_extra = {
        "band": _band(page),
        "dev_stage_key": _dev_stage_key(page["lo"]),
        "type_counts": type_counts,
        "untyped": untyped,
        "median_price": _median_price(prices),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "stockout_count": stockout,
        "price_fetched_at": price_fetched_at,
        "picks": picks,
        "faq": build_faq(page, prices=prices, stockout=stockout, n_items=len(recs), younger_ok=younger_ok,
                         characters=characters, type_counts=type_counts, price_fetched_at=price_fetched_at,
                         older_min=older_min),
    }
    return per, page_extra


def _drop_reason_dup(fits: list[str], reason: str | None) -> list[str]:
    """カードの理由 (card_reason) が「向く人：…」のとき、同じ文を「こんな子に向く」に重ねない。"""
    if not reason:
        return fits
    r = reason.removeprefix("向く人：").strip()
    return [f for f in fits if f != r]


def _top3(recs, per: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """迷ったらこの 3 つ: 総合 1 位 / 声がいちばん厚い / いちばん安くなっている (無ければ いちばん手ごろ)。"""
    if not recs:
        return []
    pos = {r.asin: i + 1 for i, r in enumerate(recs)}
    picks = [{"slot": "overall", "label": "総合 1 位", "asin": recs[0].asin, "note": ""}]
    used = {recs[0].asin}
    rest = [r for r in recs if r.asin not in used]
    voiced = [r for r in rest if per[r.asin]["voice_sources"] > 0]
    if voiced:
        best = max(voiced, key=lambda r: (per[r.asin]["voice_sources"], -pos[r.asin]))
        picks.append({"slot": "voices", "label": "声がいちばん厚い", "asin": best.asin,
                      "note": f"声の出典 {per[best.asin]['voice_sources']} 件"})
        used.add(best.asin)
    rest = [r for r in recs if r.asin not in used]
    dropped = [r for r in rest if per[r.asin]["price"]]
    if dropped:
        best = max(dropped, key=lambda r: (per[r.asin]["price"]["drop_yen"], -pos[r.asin]))
        picks.append({"slot": "cheaper", "label": "いちばん安くなっている", "asin": best.asin,
                      "note": f"ふだんより{per[best.asin]['price']['drop_yen']:,}円安い"})
    elif rest:
        priced = [r for r in rest if isinstance(r.best_price, (int, float)) and r.best_price > 0]
        if priced:
            best = min(priced, key=lambda r: (r.best_price, pos[r.asin]))
            picks.append({"slot": "cheapest", "label": "いちばん手ごろ", "asin": best.asin, "note": ""})
    for p in picks:
        p["rank"] = pos[p["asin"]]
    return picks
