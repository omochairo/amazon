"""
score_per_asin_info.py  (#1600 Phase 1)

ASIN ごとの「第三者情報量スコア」を data/raw/per_asin/<ASIN>/ の事前収集物から
算出する。Jules は記事生成時にこの per_asin 配下のみを一次ソースとして参照するが、
公式/レビューメディアの一部 ASIN は news/youtube/books が事実上ゼロで、Jules が
google_search 任せ → 出典の薄い水増し記事 (#1599 sources floor 割れ) を生む。

このスクリプトは「真に情報ゼロの品 (Bajoy/Joyreal 級)」を定量化し、
  1. 03-invoke-jules の ASIN 選定で band=="zero" を defer/skip (concurrent 枠占有 prevent, #1353)
  2. Jules プロンプトに情報量を動的注記 (薄い品は正直優先・水増し禁止)
に使う。quality_gate の floor 自体は変えない (合格基準は不変、入口で間引くだけ)。

スコアは「この商品そのものについての第三者材料がどれだけ事前収集できているか」を測る。
カテゴリ文脈にすぎない competitors は弱い補助、ブランド tier は google_search の
当てになりやすさ (S/A は公式情報豊富) のフォールバック信頼度として扱う。

#5490 案B: third_party_sources.json (Tavily pre-fetch, #1600 Phase 2) の非販売
distinct host も見る。これを見ないと「素材を足す経路が defer を解除できない」
デッドロックになる (fetch_third_party_sources は --bands zero,thin を対象に走るのに、
その成果が band に反映されないので永久に zero のまま = 03-invoke-jules が拾わない)。
ただし信号としては弱いので evidence には入れず、zero を外す判定にだけ使う
(下の _THIN_EVIDENCE_CEILING 付近のコメント参照)。

Usage:
    python scripts/score_per_asin_info.py B0FX2RNS7J
    python scripts/score_per_asin_info.py --all            # 全 per_asin を JSONL で
    python scripts/score_per_asin_info.py B0... --json      # 1 件を pretty JSON で
"""

from __future__ import annotations

import argparse
import functools
import json
import pathlib
import re
import sys
import urllib.parse

try:
    import brand_normalizer
    import market_prices
    import source_sites
    from filter_raw_per_asin import exclude_title_only
except ImportError:  # スクリプトを scripts/ 外から呼ぶ場合のフォールバック
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import brand_normalizer  # type: ignore
    import market_prices  # type: ignore
    import source_sites  # type: ignore
    from filter_raw_per_asin import exclude_title_only  # type: ignore

RAW_DIR = pathlib.Path("data/raw")
PER_ASIN_DIR = RAW_DIR / "per_asin"
FIRST_PARTY_SOURCES = pathlib.Path("data/analytics/first_party_sources.json")
ARTICLES_DIR = pathlib.Path("data/articles")

# #9199 案(b): omcha.jp (おもちゃいろ本家) の実使用記事は「運営者の一次情報」として
# 非販売ソースに 1 件まで数える。第三者ではないので 2 件目は外部から要る。
# navi.omcha.jp (このサイト自身) は数えない: 自分の生成記事を根拠にする循環 (#6593)。
_FIRST_PARTY_HOSTS = ("omcha.jp", "www.omcha.jp")
_FIRST_PARTY_MAX_COUNTED = 1

# ニュース見出しの末尾 " - 媒体名" を媒体として distinct 集計する。
# Google News RSS は url が news.google.com 固定リダイレクトのため host では
# 媒体を区別できず、唯一 title 末尾のパブリッシャ名だけが distinct 信号になる。
_NEWS_SOURCE_SEP = " - "

# スコア配点 (合計 max 112)。
#   news_sources : distinct 媒体数      max 40 (1 媒体 8 点 / 上限 5)
#   youtube      : 動画件数             max 20 (1 件 5 点 / 上限 4)
#   books        : 書籍件数             max 10 (1 件 5 点 / 上限 2)
#   competitors  : 競合件数 (弱信号)    max 10 (1 件 2 点 / 上限 5)
#   third_party  : distinct host (弱信号) max 12 (1 host 4 点 / 上限 3)
#   brand_tier   : フォールバック信頼度 max 20 (S20/A16/B12/C8/D4)
_TIER_FALLBACK = {"S": 20, "A": 16, "B": 12, "C": 8, "D": 4}
_THIRD_PARTY_POINT = 4
_THIRD_PARTY_CAP = 3

# #5490 案B: third_party_sources.json (Tavily pre-fetch, #1600 Phase 2) の
# 非販売 distinct host がこの数以上あれば zero から外す。
#
# なぜ 2 か: band=="zero" が主張しているのは「v5 §6.5.1 の非販売ソース 2 件を
# 満たす材料が per_asin に無い」であって「情報が少ない」ではない。閾値を 2 に
# 揃えることで、band の意味と実際のゲート条件が一致する。
_THIRD_PARTY_MIN_HOSTS = 2

# band 判定。evidence = news/youtube/books の合算点 (商品そのものの第三者材料)。
# zero  : 商品固有の第三者材料が皆無 (evidence==0) かつ tier が D/unknown かつ
#         third_party の非販売 host も _THIRD_PARTY_MIN_HOSTS 未満
#         = google_search のフォールバックも事前収集も弱い → defer 対象
# thin  : evidence は乏しいが tier 信頼 or 僅かに材料あり → 生成は通すが正直注記
# ok    : 十分な材料あり
#
# third_party は **evidence に入れない** (total にだけ足す)。理由:
# Tavily は検索結果をそのまま拾うので個々の URL が本当にその商品の話かは未検証で、
# distinct 媒体が確認できている news や、filter_raw_per_asin の strict 判定を
# 通った youtube より弱い信号でしかない。evidence に混ぜると thin→ok が
# 実測 118 件動き (2026-08-18, 記事 2,064 件で試算)、band=="ok" で
# build_jules_prompt._info_note が「水増し禁止」から「素材は十分」へ切り替わって
# しまう。third_party に許すのは「zero (= 材料皆無) の否定」までとし、
# 「材料が潤沢 (ok)」の主張はさせない。
_THIN_EVIDENCE_CEILING = 16  # evidence がこの値以下なら "薄い" とみなす

# #9239: 事前収集 (Tavily) の候補 host のうち、非販売の第三者ソースに数えないもの。
# host の完全一致か、その subdomain に当てる (部分一致にすると無関係の実在
# ドメインまで落とす。#6593 の notomcha.jp と同じ罠)。
#
# 通販サイト: 商品ページは「その店が売っている」記載で、第三者の評価ではない。
# 非販売に数えると「非販売 2 件揃った」と誤判定して生成に回る (#9193 B0CGLGLJRM:
# 候補 7 host が全部通販)。gate (quality_gate.check_sources_v5) も同じ集合で
# 非販売から外すので、SSOT は source_sites に置く (navi-brain#92)。
_RETAIL_SITE_HOSTS = source_sites.RETAIL_SITE_HOSTS
# 商品と無関係: メーカー名やブランド名で検索したときに拾う会社情報・求人・金融・
# 地図・アプリストア等。商品について何も書いていないので Jules は採用しない
# (#9211 B0DKFDMJZS: 英国法人登記と rocketreach)。合計にも数えない。
_UNRELATED_HOSTS = frozenset({
    "company-information.service.gov.uk", "find-and-update.company-information.service.gov.uk",
    "rocketreach.co", "cbinsights.com", "tracxn.com", "crunchbase.com",
    "zoominfo.com", "dnb.com", "opencorporates.com", "bizdb.co.uk",
    "indeed.com", "linkedin.com", "glassdoor.com", "talent-book.jp",
    "yelp.com", "mapquest.com", "jalan.net", "tabelog.com",
    "tradingkey.com", "finance.yahoo.com", "coingecko.com", "coinmarketcap.com",
    "weblio.jp", "play.google.com", "apps.apple.com",
})


def _load(path: pathlib.Path) -> dict | list | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _items(data) -> list:
    if isinstance(data, dict):
        v = data.get("items")
        return v if isinstance(v, list) else []
    return data if isinstance(data, list) else []


def _news_distinct_sources(news) -> int:
    sources: set[str] = set()
    for it in _items(news):
        title = (it or {}).get("title") if isinstance(it, dict) else None
        if not isinstance(title, str) or _NEWS_SOURCE_SEP not in title:
            continue
        src = title.rsplit(_NEWS_SOURCE_SEP, 1)[-1].strip()
        # 末尾の "(...)" 補足 (例 "Rolling Stone Japan(ローリングストーン...)") を畳む
        src = re.sub(r"[(（].*$", "", src).strip()
        if src:
            sources.add(src)
    return len(sources)


# タイトルのトークン分割用 (全角/半角スペース・各種括弧・読点)。
_TOKEN_SPLIT = re.compile(r"[\s　\[\]【】（）()『』「」、,/|]+")
_TIER_RANK = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}


# 検索結果ページ判定。検索語を URL に埋めただけのページは「誰かがその商品について
# 書いたもの」ではないので、第三者ソースとして数えてはいけない。
#
# fetch_third_party_sources._SEARCH_ENGINE_SUBSTR は Google/Bing 等の汎用エンジンしか
# 見ておらず、価格比較・EC の検索 URL が素通りしていた。実測 (2026-08-18, 収集済み
# 1,414 ASIN / 6,577 URL): search.kakaku.com だけで 409 件 = 全 host 中 3 位。
# path 型 (/search/<検索語>) と query 型 (?q=/?keyword=) を併せて約 510 件 (7.7%)。
#
# fetch 側を直しても既に書かれた JSON は残るので、採点時にも必ず落とす。
# fetch_third_party_sources はこの関数を import して同じ判定を使う (SSOT)。
_SEARCH_QUERY_PARAMS = frozenset({"q", "query", "keyword", "keywords"})


def is_search_result_url(url: str) -> bool:
    """URL が検索結果ページなら True (第三者ソースとして数えない)。"""
    try:
        parts = urllib.parse.urlparse((url or "").strip().lower())
    except ValueError:
        return True  # 壊れた URL は数えない
    if not parts.netloc:
        return True
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    if host.startswith("search."):
        return True
    if "/search" in parts.path:
        return True
    try:
        params = urllib.parse.parse_qs(parts.query)
    except ValueError:
        return False
    return bool(_SEARCH_QUERY_PARAMS & set(params))


_host_in = source_sites.host_in


def host_kind(host: str) -> str:
    """事前収集の候補 host の種別 (#9239): "third_party" / "retail" / "unrelated"。

    fetch_third_party_sources も同じ判定を使う (SSOT はこちら)。
    """
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if _host_in(host, _UNRELATED_HOSTS):
        return "unrelated"
    if _host_in(host, _RETAIL_SITE_HOSTS):
        return "retail"
    return "third_party"


def _candidate_hosts(asin: str, base: pathlib.Path) -> dict[str, set[str]]:
    """third_party_sources.json の「検索結果ページでない」distinct host を種別ごとに返す。

    fetch 側も host 単位で dedupe しているが、採点側でも数え直す (古い JSON や
    手で足された行を信用しない)。host フィールドが無ければ URL から補う。
    """
    out: dict[str, set[str]] = {"third_party": set(), "retail": set(), "unrelated": set()}
    data = _load(base / asin / "third_party_sources.json")
    srcs = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(srcs, list):
        return out
    for src in srcs:
        if not isinstance(src, dict):
            continue
        url = src.get("url") or ""
        if not url or is_search_result_url(url):
            continue
        host = (src.get("host") or "").strip().lower()
        if not host:
            host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            out[host_kind(host)].add(host)
    return out


def _third_party_hosts(asin: str, base: pathlib.Path) -> int:
    """非販売の第三者として数えられる distinct host 数 (通販・無関係を除く。#9239)。"""
    return len(_candidate_hosts(asin, base)["third_party"])


# 判定は照合 (fetch_cross_search) と価格表示 (build_post) でも使うので market_prices に置く (#9244)
other_asin_in_url = market_prices.other_asin_in_url


@functools.lru_cache(maxsize=4)
def _matched_asins(path: str) -> frozenset:
    """rakuten_matched / yahoo_matched で商品ページが特定できている ASIN (1 プロセス 1 回)。"""
    data = _load(pathlib.Path(path))
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return frozenset()
    return frozenset(r["matched_asin"] for r in rows
                     if isinstance(r, dict) and r.get("matched_asin") and r.get("url")
                     and not other_asin_in_url(r["url"], r["matched_asin"]))


def mall_pages(asin: str, raw_dir: pathlib.Path = RAW_DIR) -> int:
    """sources に入れられるモールの商品ページ数 (#9239): Amazon 1 + 楽天・Yahoo の照合済み。

    Amazon の商品ページ (dp) は ASIN から必ず作れる。楽天・Yahoo は照合済みの
    ときだけ。レビューページは数えない (中身を Jules が読むかは分からない)。
    """
    n = 1
    for name in ("rakuten_matched.json", "yahoo_matched.json"):
        if asin in _matched_asins(str((pathlib.Path(raw_dir) / name).resolve())):
            n += 1
    return n


@functools.lru_cache(maxsize=4)
def _first_party_index(path: str) -> dict:
    """first_party_sources.json を ASIN -> 実使用記事 URL のタプルに引き直す (1 プロセス 1 回)。

    role=primary (その記事の主役の商品) だけ。compared は比較で名前が出るだけで、
    その商品の一次情報とは言えない。無い/壊れていれば空 (pick を止めない)。
    """
    data = _load(pathlib.Path(path))
    rows = data.get("sources") if isinstance(data, dict) else None
    out: dict[str, list[str]] = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("role") != "primary":
            continue
        url = r.get("post_url") or ""
        if urllib.parse.urlparse(url).netloc.lower() not in _FIRST_PARTY_HOSTS:
            continue
        lst = out.setdefault(r.get("asin") or "", [])
        if url not in lst:
            lst.append(url)
    return {a: tuple(v) for a, v in out.items()}


@functools.lru_cache(maxsize=4)
def _article_index(articles_dir: str) -> dict:
    """ASIN -> その ASIN の最新の記事 JSON のパス (slug 日付が最大のもの)。"""
    out: dict[str, pathlib.Path] = {}
    d = pathlib.Path(articles_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):  # slug は日付始まりなので昇順 = 古い順
        m = re.match(r"^\d{4}-\d{2}-\d{2}-(B0[A-Z0-9]{8})\.json$", p.name)
        if m:
            out[m.group(1)] = p
    return out


def prior_article_urls(asin: str, articles_dir: pathlib.Path | None = None) -> tuple:
    """この ASIN の既存記事が sources に載せている非販売の URL (navi-brain#92)。

    書き直しのとき、前の記事が見つけた非販売の出典は Jules にとって材料になる
    (build_jules_prompt が候補として渡す)。前の記事は「本文から参照していない」だけで
    非販売を 2 サイト以上持っていることが多く、それなら書き直しで参照を付ければ足りる。
    販売ページ・通販サイト・検索結果・自社サイト (omcha.jp は first_party_posts で
    別に数える) は除く。記事の無い ASIN は空。
    """
    # 既定は呼び出し時に読む (テストが ARTICLES_DIR を差し替えられるように)
    d = ARTICLES_DIR if articles_dir is None else articles_dir
    path = _article_index(str(pathlib.Path(d).resolve())).get(asin)
    data = _load(path) if path else None
    srcs = data.get("sources") if isinstance(data, dict) else None
    out = []
    for src in srcs if isinstance(srcs, list) else []:
        url = src.get("url") if isinstance(src, dict) else None
        if not isinstance(url, str) or not url or is_search_result_url(url):
            continue
        host = source_sites.host_of(url)
        if (not host or host_kind(host) != "third_party"
                or source_sites.registered_domain(host) == "omcha.jp"):
            continue
        if url not in out:
            out.append(url)
    return tuple(out)


def first_party_posts(asin: str, path: pathlib.Path = FIRST_PARTY_SOURCES) -> tuple:
    """この ASIN を主役にした omcha.jp の実使用記事 URL (#9199 案b)。"""
    # cwd 相対のまま cache のキーにすると、cwd を変えたときに別の索引を返す
    return _first_party_index(str(pathlib.Path(path).resolve())).get(asin, ())


def _brand_tier(amazon) -> str:
    """raw amazon.json item から brand tier を推定する。

    記事 JSON と違い raw item には独立した `brand` フィールドが無いため、
    title をトークン分割して各トークン (および先頭 2 トークン連結) を
    brand_normalizer に通す。レゴ等の短い日本語ブランド名は fuzzy 対象外
    (len>=4 要件) だが、単独トークンなら exact 一致するため拾える。
    複数ヒット時は最も信頼度の高い (tier rank 最大) ものを採用。
    """
    item = {}
    if isinstance(amazon, dict):
        item = amazon.get("item") if isinstance(amazon.get("item"), dict) else amazon
    title = item.get("title") if isinstance(item, dict) else ""
    seller = item.get("seller") if isinstance(item, dict) else ""
    tokens = [t for t in _TOKEN_SPLIT.split(title or "") if t]
    candidates = list(tokens)
    if len(tokens) >= 2:
        candidates.append(tokens[0] + tokens[1])  # "タカラトミー アーツ" 等の分割語
    if seller:
        candidates.append(seller)
    best = "D"
    for c in candidates:
        tier = brand_normalizer.normalize(c).tier
        if _TIER_RANK.get(tier, 1) > _TIER_RANK.get(best, 1):
            best = tier
    return best


def score_asin(asin: str, base: pathlib.Path = PER_ASIN_DIR) -> dict:
    d = base / asin
    news = _load(d / "news.json")
    youtube = _load(d / "youtube.json")
    books = _load(d / "books.json")
    competitors = _load(d / "competitors.json")
    amazon = _load(d / "amazon.json")

    # filter_raw_per_asin が走った痕跡 (news/youtube/books のいずれかのファイル) があるか。
    # 痕跡が無い ("未 fetch") のと「fetch 済みで空」は意味が違う: 前者は enrich すべきで
    # あって defer 対象ではない。band=="zero" (defer 信号) は fetch 済みかつ真ゼロのみに限定。
    evidence_fetched = any(
        (d / f"{k}.json").exists() for k in ("news", "youtube", "books")
    )

    news_src = _news_distinct_sources(news)
    # _match: "title_only" (#8162 案A) は裏付け語なしで一般語/シリーズ名の
    # ASIN に混ざりうる誤りなので、enrich/defer の優先度判断の材料にしない。
    yt = len(_items(exclude_title_only(youtube)))
    bk = len(_items(books))
    comp_list = competitors.get("competitors", []) if isinstance(competitors, dict) else []
    comp = len(comp_list) if isinstance(comp_list, list) else 0
    tier = _brand_tier(amazon)

    pt_news = min(news_src, 5) * 8
    pt_yt = min(yt, 4) * 5
    pt_bk = min(bk, 2) * 5
    pt_comp = min(comp, 5) * 2
    pt_tier = _TIER_FALLBACK.get(tier, 4)
    cand = _candidate_hosts(asin, base)
    tp_hosts = len(cand["third_party"])
    # navi-brain#92: gate と同じ「サイト」単位 (登録ドメイン・SNS はまとめて 1)
    tp_site_keys = {source_sites.site_key(h) for h in cand["third_party"]}
    tp_sites = len(tp_site_keys)
    # 既存記事の非販売の出典のうち、事前収集の候補に無いサイト (書き直しの材料)。
    # google.com (Google ニュース・Books の転送 URL) は news の材料として別に数える
    prior_keys = {source_sites.site_key(source_sites.host_of(u)) for u in prior_article_urls(asin)}
    prior_extra = len(prior_keys - tp_site_keys - {"google.com"})
    pt_third = min(tp_hosts, _THIRD_PARTY_CAP) * _THIRD_PARTY_POINT

    evidence = pt_news + pt_yt + pt_bk
    total = evidence + pt_comp + pt_tier + pt_third

    # 未知ブランドも brand_normalizer は tier="D" を返すので D 判定で unknown も拾える。
    #
    # #5490 案B: third_party の非販売 host が 2 以上あれば zero にしない。
    # ここを塞がないと「素材を足す経路 (fetch_third_party_sources は --bands zero,thin
    # を対象に走る) が、素材不足を理由にした defer を永久に解除できない」デッドロックに
    # なる。band が動かない限り 03-invoke-jules は拾わず、記事は古いまま残り続ける。
    #
    # unfetched の判定は変えない。news/youtube/books 自体が未収集なら、third_party が
    # あっても先に本流の収集を回すべきで、defer 対象ではない (enrich 待ち) のは同じ。
    if not evidence_fetched and evidence == 0:
        band = "unfetched"  # fetch 未実行 → enrich すべき。defer 対象ではない
    elif evidence == 0 and tier == "D" and tp_sites + prior_extra < _THIRD_PARTY_MIN_HOSTS:
        band = "zero"  # fetch 済みで真ゼロ かつ フォールバックも事前収集も弱い → defer
    elif evidence <= _THIN_EVIDENCE_CEILING:
        band = "thin"
    else:
        band = "ok"

    return {
        "asin": asin,
        "info_score": total,
        "evidence_score": evidence,
        "band": band,
        "evidence_fetched": evidence_fetched,
        "brand_tier": tier,
        "news_sources": news_src,
        "youtube": yt,
        "books": bk,
        "competitors": comp,
        "third_party_hosts": tp_hosts,
        "third_party_sites": tp_sites,
        "prior_article_sites": prior_extra,
        "retail_hosts": len(cand["retail"]),
        "unrelated_hosts": len(cand["unrelated"]),
        "mall_pages": mall_pages(asin, base.parent),
        "third_party_fetched": (d / "third_party_sources.json").exists(),
        "first_party_posts": len(first_party_posts(asin)),
        "exists": d.is_dir(),
    }


def non_sales_material(result: dict) -> int:
    """v5 §6.5.1 の「非販売の出典」に使える材料のサイト数 (score_asin の戻り値から)。

    事前収集 (fetch_third_party_sources) の非販売サイトと、news の distinct 媒体を
    足す。youtube / books は数えない: youtube は何本あっても SNS で 1 サイトにしか
    ならず、books は販売サイト由来が多く、どちらも「第三者の非販売サイト」の
    裏付けとしては弱い。

    navi-brain#92: 事前収集は host ではなくサイト (登録ドメイン・SNS はまとめて 1) で
    数える。gate がサイトで数えるので、host で数えると ja/en の wikipedia で「2 件
    揃った」と判定して生成に回り、gate で落ちる。third_party_sites の無い古い結果は
    host 数で代用する。

    news は媒体がいくつあっても 1 サイトまで。news.json の url は Google ニュースの
    転送 URL (news.google.com) で、Jules はそれをそのまま出典に入れる (2026-10 の記事で
    実測)。gate では google.com の 1 サイトにしかならない。

    #9199 案(b): omcha.jp の実使用記事 (first_party_posts) を 1 件まで足す。
    プロンプトにも sources に 1 件まで採用してよいと渡している (build_jules_prompt)。

    既存記事がある ASIN (書き直し) は、前の記事の非販売の出典のうち事前収集の候補に
    無いサイト (prior_article_sites) も足す。build_jules_prompt が候補として渡す。
    """
    fp = min(result.get("first_party_posts", 0), _FIRST_PARTY_MAX_COUNTED)
    sites = result.get("third_party_sites", result.get("third_party_hosts", 0))
    news = min(result.get("news_sources", 0), 1)
    return sites + result.get("prior_article_sites", 0) + news + fp


def awaiting_sources(result: dict) -> bool:
    """非販売の出典が 2 サイト揃うまで生成を待たせる状態か (#9199)。

    thin / unfetched は「材料は乏しいが書ける」扱いで生成に回していたが、
    非販売の材料が 2 件に届かないまま生成すると sources_v5 で確実に落ちる
    (実測: 書き直し待ち 12 件中 8 件が第三者ソース 0 件・defer されず)。
    落とすのではなく、34-third-party-sources が集めるまで待たせる。
    ok は evidence (news/youtube/books) が十分あるので待たせない。

    navi-brain#92: gate から合計 5 件の条件が外れたので、合計 5 件に届く材料が
    あるか (#9239) は見ない。
    """
    if result.get("band") not in ("thin", "unfetched"):
        return False
    return non_sales_material(result) < _THIRD_PARTY_MIN_HOSTS


def should_defer(result: dict) -> bool:
    """03-invoke-jules の pick から外すか (score_asin の戻り値で判定)。

    zero は従来どおり外す (#1600)。unfetched は「本流の収集待ち」で defer 対象では
    なかったが、ranking_pool (楽天ランキング由来) の ASIN は amazon.json の items[] に
    居ないので news/youtube/books が永久に集まらず、ずっと unfetched のまま pick される。
    素材が amazon.json と competitors だけでは sources 5 件 (v5 §6.5.1) に届かず、
    navi-brain#89 以降の Jules は削らずに validate 赤のまま終えるので PR が滞留する
    (実測 2026-10-05: ranking-sniper の 4 件が全部 unfetched で初回 validate 落ち、
    3 件が赤のまま放置)。

    #9199: thin も同じ。非販売の材料が 2 件に届くまで待たせる (awaiting_sources)。
    """
    if result.get("band") == "zero":
        return True
    return awaiting_sources(result)


def sources_exhausted(result: dict) -> bool:
    """待っても非販売ソースが揃う見込みが無いか (#9199)。

    defer 対象で、かつ事前収集を一度は試した (third_party_sources.json がある)。
    収集前の defer は「待てば揃うかもしれない」ので待たせる。収集後もまだ足りないなら、
    fetch_third_party_sources は空振りを長期間 (既定 180 日〜) 引き直さないので、
    実質これ以上は増えない。
    """
    return should_defer(result) and bool(result.get("third_party_fetched"))


def _cli() -> int:
    ap = argparse.ArgumentParser(description="per_asin 第三者情報量スコア (#1600 Phase 1)")
    ap.add_argument("asin", nargs="?", help="単一 ASIN")
    ap.add_argument("--all", action="store_true", help="全 per_asin を JSONL 出力")
    ap.add_argument("--json", action="store_true", help="単一 ASIN を pretty JSON 出力")
    ap.add_argument("--base", default=str(PER_ASIN_DIR), help="per_asin ベースディレクトリ")
    args = ap.parse_args()
    base = pathlib.Path(args.base)

    if args.all:
        if not base.is_dir():
            print(f"no such dir: {base}", file=sys.stderr)
            return 1
        for d in sorted(base.iterdir()):
            if not d.is_dir() or not re.match(r"^B0[A-Z0-9]{8}$", d.name):
                continue
            print(json.dumps(score_asin(d.name, base), ensure_ascii=False))
        return 0

    if not args.asin:
        ap.error("ASIN または --all が必要です")
    result = score_asin(args.asin, base)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
