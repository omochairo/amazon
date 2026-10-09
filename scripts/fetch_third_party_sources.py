"""
fetch_third_party_sources.py  (#1600 Phase 2)

Tavily Search API で per_asin の商品名キーワードを検索し、
非販売 (= レビュー / 解説 / メディア) の第三者ソース URL を pre-fetch して
data/raw/per_asin/<ASIN>/third_party_sources.json に書き出す。

#1600 の根本原因は、公式/レビューメディアの一部 ASIN が per_asin に第三者材料を
持たず、Jules が google_search 任せで出典の薄い記事 (#1599 sources floor 割れ) を
書くこと。Phase 1 (score_per_asin_info) は真ゼロ品を defer するだけだが、本 Phase 2 は
「実在し検証可能な非販売候補 URL」を先回りで収集し、Jules の裏取り精度を底上げする。

検索 backend は当初 Google CSE を採用したが、2026-01-20 の仕様変更で新規エンジンの
「ウェブ全体検索」が廃止されたため Tavily に切替 (#1600 session 102)。Tavily は
エージェント/RAG 用途前提で GitHub Actions の共有 IP からも安定して叩ける。

レーンは 2 本ある。母集合が違うだけで、収集・保存の処理は共通:

  --pool     新規候補レーン。母集合は「まだ記事になっていない ASIN」(_pickable_pool)
             で、そこから band が薄いものを拾う。既存の挙動。
  --from-gsc 既存記事レーン (#5490 案B / brain#13 2-3)。母集合を GSC の需要
             (直近 4 週の imp) で差し替える。_pickable_pool は `cand - existing` で
             既存記事を除外しているため、リライト対象には Tavily が一度も走らない
             状態だった。ここで単に `- existing` を外すと母集合が 1 本のプールに
             融合し、既存記事が --max-queries を食い尽くして新規候補レーンが飢える
             (デッドロックではないので気づきにくい)。なので母集合ごと差し替える
             別フラグにし、--max-queries も別に持たせる。

  band を抽出条件にしないのは、thin がコーパスの 3/4 を占めていて選別になって
  いないため。需要 (imp) で並べれば zero も自然に上位へ入る。

quota: Tavily 無料枠は 1,000 query/月 (≈ 33/日)。freshness skip (既定 30日) で
定常呼び出しは thin/zero ASIN 数に収まる。--max-queries で日次上限を切る。
secret (TAVILY_API_KEY) 未設定時は inert (no-op, exit 0)。

  消費の数え方は**実 API 呼び出し回数**。`record_call` が呼び出しの直前に
  `data/raw/per_asin/_tavily_usage.json` へ 1 回ぶんを刻み、--monthly-budget と
  日次上限の両方がこれを見る。成功件数 (= 書き出した third_party_sources.json の
  数) で数えていたときは、**API を叩いてから落ちた呼び出しが上限に載らず**、
  credit だけ消えてループが余分に回っていた。

  空振り (非販売 host が _THIRD_PARTY_MIN_HOSTS 未満) の ASIN は取得しても
  --from-gsc の母集合 (third_party_hosts < 2) から出ていかないので、通常の
  --max-age-days では同じ空振りを周期ごとに全件引き直す。--empty-max-age-days
  (既定 180 日, 連続空振り回数だけ倍) でネガティブキャッシュし、再問い合わせを
  後ろへ倒す。

env:
  TAVILY_API_KEY  Tavily Search API キー (https://app.tavily.com — CC 不要・無料枠)

Usage:
  python scripts/fetch_third_party_sources.py B0FX2RNS7J          # 単一 ASIN
  python scripts/fetch_third_party_sources.py --pool --max-queries 30
  python scripts/fetch_third_party_sources.py --from-gsc --max-queries 20
  python scripts/fetch_third_party_sources.py B0... --dry-run     # API を叩かず計画表示
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_cross_search import extract_search_keyword  # noqa: E402
import score_per_asin_info as _sc  # noqa: E402
import self_domain as _self_domain  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_third_party_sources")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
TAVILY_ENDPOINT = "https://api.tavily.com/search"
OUT_NAME = "third_party_sources.json"
# 実 API 呼び出し回数の共有記録 (レーン間・run 間)。per_asin 直下のファイルなので
# glob("*/" + OUT_NAME) には掛からず、workflow の `git add data/raw/per_asin` で
# main に載って次 run へ引き継がれる。
USAGE_NAME = "_tavily_usage.json"
GSC_BY_PAGE = pathlib.Path("data/analytics/history/gsc_by_page.jsonl")

# 販売/マーケットプレイス host (= 価格・購入ページ)。第三者「出典」には使わないので除外。
# レビュー価値のある価格比較 (kakaku 等) は残す: ユーザーレビューが裏取りに有用。
_RETAIL_HOST_SUBSTR = (
    "amazon.co.jp", "amazon.com", "amzn.to", "amzn.asia",
    "rakuten.co.jp", "rakuten.com", "r10.to",
    "shopping.yahoo.co.jp", "store.shopping.yahoo.co.jp", "paypaymall",
    "mercari.com", "jp.mercari", "fril.jp", "rakuma",
    "aupay.wowma.jp", "wowma.jp", "qoo10.jp", "dmm.com",
    "shop", "store.", "cart",  # 汎用 EC サブドメイン (緩め)
)
# 検索エンジンの結果ページ (#1599 で弾く対象)。出典 URL にしてはいけない。
#
# #5490 案B: この tuple は汎用エンジンしか列挙しておらず、価格比較/EC の検索 URL が
# 素通りしていた (実測 2026-08-18: 収集済み 6,577 URL のうち search.kakaku.com が
# 409 件で全 host 中 3 位、path/query 型と併せて約 510 件 = 7.7%)。構造で判定する
# score_per_asin_info.is_search_result_url を併用して塞ぐ (判定の SSOT は向こう側)。
_SEARCH_ENGINE_SUBSTR = (
    "google.com/search", "google.co.jp/search", "bing.com/search",
    "search.yahoo", "duckduckgo.com",
)
# 「お問い合わせ」ページ。商品名で検索して拾うのは無関係なサイトの問い合わせ窓口で
# (#9199 実測: シナモロールのクエリで大学と防災 NPO の問い合わせページ)、
# 商品について何も書いていない。
_CONTACT_PATH_RE = re.compile(
    r"/(?:contact(?:[-_]?(?:us|form))?|inquiry|inquiries|toiawase|otoiawase)(?:[/.?#]|$)")

# Tavily の exclude_domains / country は使わない (#9199 実測 2026-10-09)。通販と
# 無関係 host 33 件を exclude_domains に渡すと、同じクエリで raw が 20 件 → 0〜9 件に
# 減った (3 ASIN 中 2 件が 0 件)。country=japan は 3 ASIN で候補 +1 / ±0 / -2 と
# 改善しなかった。代わりに raw を 20 件取り、_is_relevant で絞る。

# 自サイト (第三者ではない)。判定は self_domain に共通化した (#6593)。
# 以前は URL 全体の部分一致だったので `notomcha.jp` のような **無関係の実在
# ドメインまで落としていた** (`"omcha.jp" in "notomcha.jp"` は真)。
# host の suffix 一致に変えて誤除外をやめる。

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
# 商品ページ URL から ASIN を復元する (slug は小文字)。ハブ/一覧は対象外。
_PRODUCT_PAGE_RE = re.compile(r"/products/(b0[a-z0-9]{8})/?(?:[?#]|$)", re.IGNORECASE)


def _host(url: str) -> str:
    """URL から host を取り出し、先頭の "www." だけを落とす。

    `lstrip("www.")` は**文字集合**を削るので、"w" や "." で始まる host の
    先頭文字まで食う (walmart.com → almart.com / watch.impress.co.jp →
    atch.impress.co.jp)。ここで作った host は third_party_sources.json に
    保存され、build_post の source_highlights が読者に出典として表示し、
    _HIGHLIGHT_HOST_DENY の照合にも使われるので、削るのは接頭辞だけにする。
    score_per_asin_info._third_party_hosts と同じ正規化。
    """
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _exclude_reason(url: str) -> Optional[str]:
    """URL を候補から外す理由を返す。外さないなら None。

    理由は third_party_sources.json の ``dropped`` に件数で残す (#9199: raw 10 件が
    候補 3〜4 件に減る段の内訳を、後から確かめられるようにする)。
    """
    low = (url or "").lower()
    if not low.startswith("http"):
        return "not_http"
    if _sc.is_search_result_url(low):  # #5490 案B: 検索結果ページを構造で弾く
        return "search_result"
    if _self_domain.is_self_domain(low):  # #6593: 自社記事を第三者ソースにしない
        return "self_domain"
    # #9239: 会社情報・求人・金融など商品と無関係な host は候補枠を食うだけなので取らない。
    # navi-brain#92: 通販サイトも取らない。以前は sources の合計 5 件の足しに残していたが、
    # gate から合計件数の条件が外れ、通販は非販売にも数えない。候補枠を第三者に空ける
    # (残すと通販だけの ASIN が空振りに数えられず、再検索が後ろへ倒れない)。
    kind = _sc.host_kind(_host(low))
    if kind == "unrelated":
        return "unrelated"
    if kind == "retail":
        return "retail"
    if any(sub in low for sub in _RETAIL_HOST_SUBSTR):
        return "retail"
    if any(sub in low for sub in _SEARCH_ENGINE_SUBSTR):
        return "search_result"
    if _CONTACT_PATH_RE.search(urllib.parse.urlparse(low).path):
        return "unrelated"
    return None


# 関連度の判定 (#9199)。ニッチな商品では Tavily が商品名と関係の無いページで
# 結果を埋める (自治体の「水」のページ、別商品の通販ミラー等)。sources_v5 は非販売なら
# 中身を問わず数えるので、残すとゲートは通っても出典の質が落ち、defer 判定も
# 「材料あり」と誤る。タイトルか本文にクエリの語が 1 つも出てこない結果は外す。
_QUERY_SPLIT = re.compile(r"[\s　\[\]【】（）()『』「」［］〔〕、,/|・]+")
_FOLD_RE = re.compile(r"[\s　・\-‐ー―〜~]+")
# 2 文字以下の語は一般語に当たりやすい (「木製」「水」等) ので照合に使わない。
_MIN_QUERY_TOKEN_LEN = 3
_PARTIAL_MATCH_LEN = 6


def _fold(text: str) -> str:
    """照合用に正規化する: NFKC・小文字・ひらがな→カタカナ・空白/長音/中黒を除く。

    表記揺れ (「すけるとん」と「スケルトン」、「わくわくミッション 宇宙探査セット」
    と「わくわくミッション宇宙探査セット」) を同じ文字列にするため。
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in t)
    return _FOLD_RE.sub("", t)


def _query_tokens(query: str) -> list[str]:
    out = []
    for raw in _QUERY_SPLIT.split(unicodedata.normalize("NFKC", query or "")):
        # 長さは畳む前で測る (畳むと「ゲーム」が「ゲム」の 2 文字になって使われない)
        if len(raw.strip()) < _MIN_QUERY_TOKEN_LEN:
            continue
        tok = _fold(raw)
        if tok:
            out.append(tok)
    return out


def _is_relevant(query: str, title: str, snippet: str) -> bool:
    """タイトルか本文にクエリの語 (3 文字以上) が 1 つでも入っていれば True。

    長い語は 6 文字の部分一致でもよい。英語表記とカタカナ表記の違い
    (「Transformers」と「トランスフォーマー」) は拾えない。

    照合できる語が無いクエリでは判定しない (True)。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return True
    hay = _fold(title) + "\n" + _fold(snippet)
    for tok in tokens:
        if tok in hay:
            return True
        # Amazon タイトルは語がつながって長い (「ファーストピックアップパズル」に対して
        # 記事側は「ピックアップパズル」)。長い語は 6 文字の部分一致でも拾う。
        # 収集済み 1 万件で、6 文字で救える分は 4 分の 3 が商品に関係するページだった。
        n = _PARTIAL_MATCH_LEN
        if len(tok) > n and any(tok[i:i + n] in hay for i in range(len(tok) - n + 1)):
            return True
    return False


def _is_excluded(url: str) -> bool:
    return _exclude_reason(url) is not None


def _load(path: pathlib.Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _product_title(asin: str, base: pathlib.Path) -> str:
    amazon = _load(base / asin / "amazon.json")
    if isinstance(amazon, dict):
        item = amazon.get("item") if isinstance(amazon.get("item"), dict) else amazon
        if isinstance(item, dict) and isinstance(item.get("title"), str):
            return item["title"]
    return ""


def tavily_search(query: str, api_key: str, num: int = 10) -> list[dict]:
    """Tavily Search API を 1 回呼び、items(raw) を返す。429/4xx は例外送出。

    戻り値は CSE 時代と同じ shape ({"link","title","snippet"}) に正規化し、
    下流の _filter_sources / 既存テストを無改修で再利用する。
    """
    body = json.dumps({
        "query": query,
        "max_results": max(1, min(num, 20)),
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    items: list[dict] = []
    for r in data.get("results", []) or []:
        if not isinstance(r, dict):
            continue
        items.append({
            "link": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("content", ""),
        })
    return items


def _filter_sources(
    raw_items: list[dict], max_sources: int, query: Optional[str] = None,
) -> list[dict]:
    """検索 raw items から非販売 distinct host を抽出 (host あたり 1 件、上位 max_sources)。

    query を渡すと、その語がタイトルにも本文にも出てこない結果を外す (#9199)。
    """
    return _filter_sources_with_drops(raw_items, max_sources, query)[0]


def _filter_sources_with_drops(
    raw_items: list[dict], max_sources: int, query: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """_filter_sources と同じ絞り込みをして、外した候補 ({host, reason}) も返す。

    reason は _exclude_reason の値か、"off_topic" (クエリの語が出てこない)、
    "duplicate_host" (同じ host の 2 件目以降)。
    """
    seen_hosts: set[str] = set()
    out: list[dict] = []
    dropped: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        link = it.get("link", "")
        reason = _exclude_reason(link)
        if reason:
            dropped.append({"host": _host(link), "reason": reason})
            continue
        if query is not None and not _is_relevant(
                query, it.get("title") or "", it.get("snippet") or ""):
            dropped.append({"host": _host(link), "reason": "off_topic"})
            continue
        h = _host(link)
        if not h or h in seen_hosts:
            dropped.append({"host": h, "reason": "duplicate_host"})
            continue
        seen_hosts.add(h)
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": link,
            "snippet": (it.get("snippet") or "").strip(),
            "host": h,
        })
        if len(out) >= max_sources:
            break
    return out, dropped


# 空振り ASIN の再問い合わせを何段まで後ろへ倒すか。empty_streak 回目の再取得は
# `--empty-max-age-days * min(streak, _EMPTY_BACKOFF_CAP)` 日後になる。
# 頭打ちを置くのは、取れるようになった時に永久に拾い直せなくなるのを避けるため。
_EMPTY_BACKOFF_CAP = 4

# 商品名と無関係な検索結果を候補から外す判定 (#9254) が入った時刻 (UTC)。これより前に
# 取った候補は無関係なページを含みうるので、2 サイトあっても 1 度は取り直す。
_RELEVANCE_FILTER_FROM = "2026-10-09T06:46"
# 非販売 2 サイトある候補の有効期限 (日)。通常の --max-age-days (90) の代わり
_SUFFICIENT_MAX_AGE_DAYS = 365


def _empty_streak(data) -> int:
    """保存済み payload から空振りの連続回数を読む。

    `empty_streak` は後から足したフィールドなので、既存ファイルには無い。無い場合は
    保存済みの `sources` から 1 回ぶんだけ復元する (0 に倒すと、既に何度も空振り
    している ASIN が導入直後に全部 1 段目からやり直しになる)。
    """
    if not isinstance(data, dict):
        return 0
    raw = data.get("empty_streak")
    if isinstance(raw, int) and raw >= 0:
        return raw
    sources = data.get("sources")
    if isinstance(sources, list) and _site_count(sources) < _sc._THIRD_PARTY_MIN_HOSTS:
        return 1
    return 0


def _site_count(sources: list) -> int:
    """非販売の第三者候補のサイト数 (navi-brain#92: 採点・gate と同じく登録ドメイン・SNS はまとめて 1)。

    古い JSON に残っている通販サイト・検索結果ページは数えない (採点側と同じ)。
    """
    sites = set()
    for src in sources:
        if not isinstance(src, dict):
            continue
        url = src.get("url") or ""
        if url and _sc.is_search_result_url(url):
            continue
        host = src.get("host") or _sc.source_sites.host_of(url)
        if host and _sc.host_kind(host) == "third_party":
            sites.add(_sc.source_sites.site_key(host))
    return len(sites)


def _is_fresh(path: pathlib.Path, max_age_days: int,
              empty_max_age_days: int = 0) -> bool:
    """この ASIN を今回 skip してよいか (= 保存済みが十分新しいか)。

    `empty_max_age_days` > 0 のとき、**空振りが続いている ASIN には長い有効期限**を
    充てる (ネガティブキャッシュ)。--from-gsc レーンの母集合は
    `third_party_hosts < 2` の ASIN なので、非販売 host が返ってこない ASIN は
    取得しても母集合から出ていかない。通常の `--max-age-days` (このレーンは 90) だと
    同じ空振りを四半期ごとに全件引き直すことになり、レーンの消費がほぼそれで埋まる。
    """
    data = _load(path)
    if not isinstance(data, dict):
        return False
    ts = data.get("fetched_at")
    if not isinstance(ts, str):
        return False
    try:
        when = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_limit = max_age_days
    if empty_max_age_days > 0:
        streak = _empty_streak(data)
        if streak > 0:
            age_limit = max(
                age_limit,
                empty_max_age_days * min(streak, _EMPTY_BACKOFF_CAP),
            )
    # navi-brain#92: 既に非販売 2 サイトある ASIN は、ほぼ取り直さない (有効期限を
    # _SUFFICIENT_MAX_AGE_DAYS に延ばす)。gate も生成前の判定も 2 サイトあれば足りるので、
    # 取り直しても増える分は使われない。git 履歴で取り直しを見ると、2 サイト以上あった
    # ものが増えた例は無く、減った例 (無関係なページを外す判定が後から入ったため) は
    # あった。その判定 (#9254) より前に取ったものは、通常の有効期限で 1 度取り直す。
    # 永久にしないのは、候補が実は使えず生成が落ち続ける ASIN に取り直しの機会を残すため。
    sites = _site_count(data.get("sources") or [])
    if ts >= _RELEVANCE_FILTER_FROM and sites >= _sc._THIRD_PARTY_MIN_HOSTS:
        age_limit = max(age_limit, _SUFFICIENT_MAX_AGE_DAYS)
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - when).days < age_limit


def _usage_path(base: pathlib.Path) -> pathlib.Path:
    return base / USAGE_NAME


def record_call(base: pathlib.Path, now: Optional[_dt.datetime] = None) -> int:
    """API を 1 回叩くことを共有カウンタに刻み、今月の累計を返す。

    **呼び出しの直前に呼ぶこと。** Tavily の credit はレスポンスが 200 で返るか
    どうかに関係なく、リクエストを投げた時点で消えうる。成功後に数えると、
    例外や 4xx で落ちた呼び出しがカウンタから抜け、budget が実消費より緩くなる。
    書き込みも 1 回ごとに行う (run が途中で落ちてもそこまでの消費が残る)。

    月が変わったら 0 から数え直す。
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    month = now.strftime("%Y-%m")
    path = _usage_path(base)
    data = _load(path)
    calls = 0
    if isinstance(data, dict) and data.get("month") == month:
        raw = data.get("calls")
        calls = raw if isinstance(raw, int) and raw >= 0 else 0
    calls += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"month": month, "calls": calls, "updated_at": now.isoformat()},
                  f, ensure_ascii=False, indent=2)
    return calls


def _fetched_at_usage(base: pathlib.Path, now: _dt.datetime) -> int:
    """今月ぶんの**成功**取得件数を、書き出し済み `fetched_at` から数える。

    これは実 API 消費の**下限**でしかない (失敗呼び出しはファイルを残さない /
    同月 2 回取得は上書きで 1 件に潰れる)。単独では budget の根拠にできないが、
    `record_call` のカウンタが無い月 (導入直後・カウンタ喪失時) の底として使う。
    """
    prefix = now.strftime("%Y-%m")
    used = 0
    for path in base.glob("*/" + OUT_NAME):
        data = _load(path)
        if not isinstance(data, dict):
            continue
        ts = data.get("fetched_at")
        if isinstance(ts, str) and ts.startswith(prefix):
            used += 1
    return used


def raw_call_count(base: pathlib.Path, now: Optional[_dt.datetime] = None) -> int:
    """今月ぶんの `_tavily_usage.json` の `calls` (実呼び出し回数) をそのまま返す。

    `month_usage` は budget 判断用に成功件数との大きい方を返すが、**ある1回の実行が
    実際に何回 Tavily を呼んだか** (差分で消費量を報告する用途) には、この生カウンタの
    差を使う。成功件数と混ぜると、成功件数側がたまたま上回る月に消費が 0 と出てしまう。
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    data = _load(_usage_path(base))
    if isinstance(data, dict) and data.get("month") == now.strftime("%Y-%m"):
        raw = data.get("calls")
        if isinstance(raw, int) and raw >= 0:
            return raw
    return 0


def month_usage(base: pathlib.Path, now: Optional[_dt.datetime] = None) -> int:
    """今月ぶんの Tavily 消費。**実呼び出し回数**と成功件数の大きい方を返す。

    Tavily 無料枠は 1,000/月。レーンが 2 本 (新規候補 / 既存記事) あり**別プロセス**で
    走るので、プロセス内のカウンタでは合算を見張れない。共有記録は 2 つある:

      1. `_tavily_usage.json` の `calls` — `record_call` が**呼び出しの直前**に刻む
         実消費。失敗した呼び出しも入るので、これが本来の budget の根拠。
      2. 各 `third_party_sources.json` の `fetched_at` — 成功件数。1 が無い月の底。

    2 は常に 1 以下になるはずだが、カウンタのファイルが失われた月 (導入直後や
    PR が落ちて main に載らなかったとき) には 1 だけが過小になる。**過小に出た側で
    budget を判断すると枠を超えて投げる**ので、両者の大きい方を採る。

    1 回の実行の消費量を報告する用途には、この関数ではなく `raw_call_count` を使うこと
    (成功件数側が上回る月に消費が 0 と出てしまうため)。
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return max(raw_call_count(base, now=now), _fetched_at_usage(base, now))


def _notice(level: str, message: str) -> None:
    """Actions の run ログに annotation を出す (ローカルでは素の log のみ)。

    #4793: 枠の枯渇や縮退が「緑のまま収集数だけ減る」形で進むと誰も気づかない。
    ログ行は 1 日 40 行に埋もれるので、UI に出る annotation を併せて出す。
    """
    getattr(logger, "warning" if level == "warning" else "info")("%s", message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::{}::{}".format(level, message), flush=True)


def fetch_for_asin(
    asin: str, api_key: str, base: pathlib.Path,
    max_sources: int = 8, dry_run: bool = False,
) -> dict:
    """1 ASIN 分の第三者ソースを収集して書き出す。戻り値は要約 dict。"""
    title = _product_title(asin, base)
    if not title:
        return {"asin": asin, "status": "skip_no_title", "sources": 0}
    query = extract_search_keyword(title)
    if dry_run:
        return {"asin": asin, "status": "dry_run", "query": query, "sources": 0}
    out_path = base / asin / OUT_NAME
    prev_streak = _empty_streak(_load(out_path))
    # credit はレスポンスを待たずに消える。例外で抜ける経路も含めて必ず数える。
    record_call(base)
    # #9199: 関連度で外す分があるので raw を多めに取る (basic は件数によらず 1 credit)
    raw = tavily_search(query, api_key, num=20)
    sources, dropped = _filter_sources_with_drops(raw, max_sources, query)
    # 空振り (非販売サイトが floor 未満) の連続回数。次回の再問い合わせを後ろへ倒す。
    streak = prev_streak + 1 if _site_count(sources) < _sc._THIRD_PARTY_MIN_HOSTS else 0
    payload = {
        "asin": asin,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "query": query,
        "engine": "tavily",
        "raw_count": len(raw),
        "empty_streak": streak,
        "sources": sources,
        # raw から候補に残らなかった分の host と理由 (#9199)
        "dropped": dropped,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"asin": asin, "status": "ok", "query": query,
            "sources": len(sources), "empty_streak": streak}


def _pickable_pool() -> list[str]:
    """03-invoke-jules と同じ候補母集合
    (amazon.json items + ranking_pool + first_party_pool + rewrite_queue 待ち
    − 既存記事 (rewrite_queue 待ちを除く))。

    first_party_pool (omcha-ops#264) は 03-invoke-jules の先頭に前置され、
    should_defer の対象外で必ず pick される。ranking_pool と同じく amazon.json の
    items[] に居ないので news/youtube/books が集まらず常に unfetched になる。
    ここで拾わないと第三者候補も永久に 0 件のまま Jules に渡り、sources 5 件
    (v5 §6.5.1) に届かず validate 赤で滞留する (実測 2026-10-06: B0FYCTJF5Z)。
    """
    cand: set[str] = set()
    raw = _load(pathlib.Path("data/raw/amazon.json"))
    if isinstance(raw, dict):
        for i in raw.get("items", []):
            a = i.get("asin") if isinstance(i, dict) else None
            if isinstance(a, str) and _ASIN_RE.match(a):
                cand.add(a)
    for pool_file in ("data/raw/ranking_pool.json", "data/raw/first_party_pool.json"):
        rp = _load(pathlib.Path(pool_file))
        if isinstance(rp, dict):
            for a in rp.get("asins", []):
                if isinstance(a, str) and _ASIN_RE.match(a):
                    cand.add(a)
    existing: set[str] = set()
    for p in pathlib.Path("data/articles").glob("*.json"):
        m = re.search(r"(B0[A-Z0-9]{8})", p.name)
        if m:
            existing.add(m.group(1))
    # rewrite_queue 待ち (#2711 / #5490) は 03-invoke-jules が既存記事でも pick する
    # (existing から外し、候補列にも足す)。こちらで既存記事として外すと第三者候補が
    # 永久に 0 件のまま書き直しが生成され、sources 5 件 (v5 §6.5.1) に届かず
    # validate 赤で滞留する (実測 2026-10-07: B0C6THPT89 / B0CKYRJ4KY)。
    # 対象は amazon.json に居ないのが常態なので、除外を外すだけでなく候補に足す。
    # pending_rewrite_candidates は band=unfetched を外すため使わない (ここが
    # unfetched を埋める側なので、それを外すと永久に集まらない)。
    try:
        import rewrite_queue as _rq
        pending = {a for a in _rq.eligible_rewrite_asins("data/articles")
                   if isinstance(a, str) and _ASIN_RE.match(a)}
    except Exception as e:  # noqa: BLE001 — 待ち行列が読めなくても本流は止めない
        logger.warning("rewrite_queue を読めない — 書き直し待ちは対象外: %s", e)
        pending = set()
    cand |= pending
    existing -= pending
    return sorted(cand - existing)


def _waiting_priority() -> dict[str, int]:
    """生成が第三者ソース待ちで止まっている ASIN の収集優先度 (小さいほど先, #9199)。

    0: first-party (omcha-ops#264)。本当に有用なおもちゃなので必ず記事にする。
       03-invoke-jules は非販売ソースが 2 件揃うまで待たせるので、ここで後回しに
       されると待ったまま記事にならない。
    1: 書き直し待ち (rewrite_queue)。first-party と同じく揃うまで待たせている。
    どちらも ``_pickable_pool`` と同じファイルから読む。読めなければ空 (本流は止めない)。
    """
    prio: dict[str, int] = {}
    try:
        import rewrite_queue as _rq
        for a in _rq.eligible_rewrite_asins("data/articles"):
            if isinstance(a, str) and _ASIN_RE.match(a):
                prio[a] = 1
    except Exception as e:  # noqa: BLE001
        logger.warning("rewrite_queue を読めない — 書き直し待ちを優先できない: %s", e)
    fp = _load(pathlib.Path("data/raw/first_party_pool.json"))
    if isinstance(fp, dict):
        for a in fp.get("asins", []):
            if isinstance(a, str) and _ASIN_RE.match(a):
                prio[a] = 0
    return prio


def _gsc_page_impressions(
    history: pathlib.Path, days: int, anchor: str | None = None,
) -> dict[str, int]:
    """gsc_by_page.jsonl の直近 `days` 日を ASIN 単位で imp 合計する。

    窓の右端は「今日」ではなくデータ側の最新日 (GSC は 2〜3 日遅れて届くので、
    今日を基準にすると窓の右端が常に空になる)。`anchor` で明示指定もできる。
    """
    rows: list[dict] = []
    try:
        with open(history, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("date"), str):
                    rows.append(r)
    except FileNotFoundError:
        logger.warning("GSC history が無い: %s", history)
        return {}
    if not rows:
        return {}
    end = anchor or max(r["date"] for r in rows)
    try:
        start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=max(1, days) - 1)).isoformat()
    except ValueError:
        logger.warning("anchor 日付が不正: %s", end)
        return {}
    imps: dict[str, int] = {}
    for r in rows:
        if not (start <= r["date"] <= end):
            continue
        m = _PRODUCT_PAGE_RE.search(r.get("page") or "")
        if not m:
            continue
        try:
            imp = int(r.get("impressions") or 0)
        except (TypeError, ValueError):
            continue
        asin = m.group(1).upper()
        imps[asin] = imps.get(asin, 0) + imp
    logger.info("GSC 窓 %s〜%s: 商品ページ %d 件", start, end, len(imps))
    return imps


def _gsc_demand_pool(
    base: pathlib.Path,
    history: pathlib.Path = GSC_BY_PAGE,
    days: int = 28,
    min_impressions: int = 10,
    anchor: str | None = None,
) -> list[str]:
    """需要 (GSC imp) を持ち、まだ第三者ソースを持っていない ASIN を imp 降順で返す。

    band では絞らない (thin がコーパスの 3/4 で選別になっていない)。imp で並べれば
    zero 帯も上位に入るし、tp_hosts>=2 になった zero は #5499 の配線で thin へ上がる。
    """
    imps = _gsc_page_impressions(history, days, anchor=anchor)
    ranked = sorted(
        ((a, v) for a, v in imps.items() if v >= min_impressions),
        key=lambda kv: (-kv[1], kv[0]),
    )
    out = [a for a, _ in ranked
           if _sc.score_asin(a, base).get("third_party_sites", 0) < _sc._THIRD_PARTY_MIN_HOSTS]
    logger.info("GSC 需要 (imp>=%d, 直近 %d 日): %d 件 / うち第三者ソース未保有 %d 件",
                min_impressions, days, len(ranked), len(out))
    return out


def _cli() -> int:
    ap = argparse.ArgumentParser(description="per_asin 第三者ソース pre-fetch (#1600 Phase 2)")
    ap.add_argument("asin", nargs="?", help="単一 ASIN")
    ap.add_argument("--pool", action="store_true",
                    help="pick 母集合のうち band が薄い ASIN をまとめて収集")
    ap.add_argument("--bands", default="zero,thin",
                    help="--pool 対象 band (カンマ区切り, 既定 zero,thin)")
    ap.add_argument("--from-gsc", action="store_true",
                    help="母集合を GSC 需要 (既存記事) に差し替える。--pool とは排他")
    ap.add_argument("--gsc-history", default=str(GSC_BY_PAGE),
                    help="--from-gsc が読む gsc_by_page.jsonl")
    ap.add_argument("--gsc-days", type=int, default=28,
                    help="--from-gsc の集計窓 (日, 既定 28 = 直近 4 週)")
    ap.add_argument("--gsc-min-impressions", type=int, default=10,
                    help="--from-gsc の imp しきい値 (既定 10)")
    ap.add_argument("--gsc-anchor", default=None,
                    help="--from-gsc の窓の右端 (既定: データ側の最新日)")
    ap.add_argument("--max-queries", type=int, default=30,
                    help="API 呼び出し日次上限 (Tavily 無料=1000/月≈33/日, 既定 30)")
    ap.add_argument("--max-sources", type=int, default=8, help="ASIN あたり保存 host 数")
    ap.add_argument("--max-age-days", type=int, default=30,
                    help="この日数以内に取得済なら skip (--force で無視)")
    ap.add_argument("--empty-max-age-days", type=int, default=180,
                    help="空振り (非販売 host が floor 未満) が続く ASIN に充てる有効期限 "
                         "(日, 既定 180)。連続空振り回数だけ倍に伸びる (上限 %d 段)。0 で無効"
                         % _EMPTY_BACKOFF_CAP)
    ap.add_argument("--monthly-budget", type=int, default=900,
                    help="今月ぶんの取得上限 (Tavily 無料=1000/月。0 で無効)。"
                         "2 本のレーンで共有し、_tavily_usage.json の実呼び出し回数で数える")
    ap.add_argument("--force", action="store_true", help="freshness を無視して再取得")
    ap.add_argument("--dry-run", action="store_true", help="API を叩かず計画のみ表示")
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    args = ap.parse_args()
    base = pathlib.Path(args.base)

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        # secret 未設定: inert に no-op で正常終了 (cron が落ちないように)
        logger.warning("TAVILY_API_KEY 未設定 — no-op で終了 (inert)")
        return 0

    if args.pool and args.from_gsc:
        # 母集合が融合すると、既存記事が --max-queries を食い尽くして新規候補レーンが
        # 飢える。レーンは必ず別々に (別 --max-queries で) 起動する。
        ap.error("--pool と --from-gsc は同時に指定できません (レーンを分けてください)")

    if args.asin:
        targets = [args.asin]
    elif args.from_gsc:
        targets = _gsc_demand_pool(
            base,
            history=pathlib.Path(args.gsc_history),
            days=args.gsc_days,
            min_impressions=args.gsc_min_impressions,
            anchor=args.gsc_anchor,
        )
    elif args.pool:
        want = {b.strip() for b in args.bands.split(",") if b.strip()}
        bands = {a: _sc.score_asin(a, base).get("band") for a in _pickable_pool()}
        targets = [a for a, b in bands.items() if b in want]
        # unfetched (= ranking_pool 品) を先頭に回す。03-invoke-jules はこれらを
        # 第三者 host が 2 件集まるまで pick しない (should_defer) ので、ここで後回しに
        # されると永久に記事にならない。ASIN の辞書順だと新しい B0H... は日次上限の外に
        # 落ちる (実測 2026-10-05: ranking 7 件中 6 件が 30 件枠の外)。安定ソートなので
        # 各グループ内の順序は従来どおり。
        #
        # #9199: さらに前に、生成が第三者ソース待ちで止まっている first-party と
        # 書き直し待ちを置く。03-invoke-jules はこれらを落とさず待たせているので、
        # 集めるのが遅れるほど記事にならない期間が延びる。
        prio = _waiting_priority()
        targets.sort(key=lambda a: (prio.get(a, 2), bands[a] != "unfetched"))
        logger.info("pool 対象 (band in %s): %d 件", sorted(want), len(targets))
    else:
        ap.error("ASIN / --pool / --from-gsc のいずれかが必要です")

    # 月次バジェット。レーンは別プロセスなので、共有記録 (_tavily_usage.json の
    # 実呼び出し回数) から実消費を数えて残枠を出す。ここで絞らないと
    # 新規 30 + 既存 20 = 50/日 = 1,500/月 が名目上の上限になり、
    # 無料枠 1,000/月 を月末前に使い切る。
    limit = args.max_queries
    if args.monthly_budget > 0 and not args.dry_run:
        used = month_usage(base)
        remaining = args.monthly_budget - used
        pct = used / args.monthly_budget * 100
        if remaining <= 0:
            _notice("warning",
                    "Tavily 月次バジェット到達: 今月 {} 件 / budget {} — 今回は 0 件で終了 "
                    "(枠切れで無言縮退させないため、意図的に何も投げない)"
                    .format(used, args.monthly_budget))
            logger.info("完了: 0 件処理 (budget 到達)")
            return 0
        if pct >= 80:
            _notice("warning",
                    "Tavily 月次バジェット {:.0f}% 消費 (今月 {} 件 / budget {}, 残 {})"
                    .format(pct, used, args.monthly_budget, remaining))
        else:
            logger.info("月次バジェット: 今月 %d 件 / budget %d (残 %d)",
                        used, args.monthly_budget, remaining)
        if remaining < limit:
            logger.info("残枠 %d < max-queries %d — 今回は残枠に合わせる", remaining, limit)
            limit = remaining

    # done = 書き出せた件数 / spent = 実際に API を投げた回数。両方で上限を切る。
    # done だけで切っていたときは、**API を叩いてから落ちた呼び出し** (429 以外の
    # HTTP エラー・タイムアウト・JSON 破損) が done に載らず、その分だけループが
    # 余分に回っていた。credit は消えているので、日次上限も実消費で見る。
    done = 0
    spent = 0
    for asin in targets:
        if done >= limit:
            logger.info("上限 (%d) 到達 — 残りは次回", limit)
            break
        if spent >= limit:
            _notice("warning",
                    "API 呼び出しが日次上限 {} に到達 (成功 {} 件) — 失敗が多い。"
                    "credit は消えているので残りは次回に回す".format(limit, done))
            break
        out_path = base / asin / OUT_NAME
        if not args.force and not args.dry_run and _is_fresh(
                out_path, args.max_age_days, args.empty_max_age_days):
            logger.info("%s: fresh skip (max-age %dd / empty-max-age %dd, streak %d)",
                        asin, args.max_age_days, args.empty_max_age_days,
                        _empty_streak(_load(out_path)))
            continue
        try:
            r = fetch_for_asin(asin, api_key, base,
                               max_sources=args.max_sources, dry_run=args.dry_run)
        except urllib.error.HTTPError as e:
            spent += 1
            if e.code in (429, 432, 433):
                # #4793 と同じ形: ここを log だけで抜けると、レーンは緑のまま
                # 収集数だけ静かに減る。UI に出して気づけるようにする。
                _notice("warning",
                        "Tavily quota/plan limit (HTTP {}) で中断 — {} 件処理した時点。"
                        "月次バジェットが実消費より緩い可能性があるので見直すこと"
                        .format(e.code, done))
                break
            logger.warning("%s: HTTP %s skip", asin, e.code)
            continue
        except Exception as e:  # noqa: BLE001 — 1 件失敗で全体を止めない
            spent += 1
            logger.warning("%s: %s skip", asin, e)
            continue
        logger.info("%s", json.dumps(r, ensure_ascii=False))
        if r.get("status") == "ok":
            spent += 1
        if r.get("status") in ("ok", "dry_run"):
            done += 1
        if not args.dry_run:
            time.sleep(1)  # Tavily への礼儀 (秒間 1 query)
    logger.info("完了: %d 件処理 / API %d 回 (max %d)", done, spent, args.max_queries)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
