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
import json
import pathlib
import re
import sys
import urllib.parse

try:
    import brand_normalizer
except ImportError:  # スクリプトを scripts/ 外から呼ぶ場合のフォールバック
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import brand_normalizer  # type: ignore

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")

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


def _third_party_hosts(asin: str, base: pathlib.Path) -> int:
    """third_party_sources.json の「検索結果ページでない」distinct host 数を返す。

    fetch 側も host 単位で dedupe しているが、採点側でも数え直す (古い JSON や
    手で足された行を信用しない)。host フィールドが無ければ URL から補う。
    """
    data = _load(base / asin / "third_party_sources.json")
    srcs = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(srcs, list):
        return 0
    hosts: set[str] = set()
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
            hosts.add(host)
    return len(hosts)


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
    yt = len(_items(youtube))
    bk = len(_items(books))
    comp_list = competitors.get("competitors", []) if isinstance(competitors, dict) else []
    comp = len(comp_list) if isinstance(comp_list, list) else 0
    tier = _brand_tier(amazon)

    pt_news = min(news_src, 5) * 8
    pt_yt = min(yt, 4) * 5
    pt_bk = min(bk, 2) * 5
    pt_comp = min(comp, 5) * 2
    pt_tier = _TIER_FALLBACK.get(tier, 4)
    tp_hosts = _third_party_hosts(asin, base)
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
    elif evidence == 0 and tier == "D" and tp_hosts < _THIRD_PARTY_MIN_HOSTS:
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
        "exists": d.is_dir(),
    }


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
