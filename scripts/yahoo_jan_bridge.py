"""楽天ランキング item の JAN を Yahoo!ショッピング経由で推定する (ranking → Amazon 紐づけ)。

楽天ランキング API は JAN を返さず、商品説明に JAN が書かれた item しか Amazon に
紐づかない (2026-10-05 実測: 30 件中 24 件が未紐づけ)。Yahoo!ショッピング
itemSearch は hits[].janCode を返すので、楽天の商品名で検索して JAN を拾い、
既存の「Amazon で JAN 完全一致」(resolve_ranking_asins.resolve_jan_to_item) に通す。

最終判定は従来どおり Amazon 側の JAN 完全一致なので、別商品のリンクを作る経路は
増えない。増えるリスクは「Yahoo のヒットが楽天の商品と別物 (色違い・セット違い)」
で、これを上位ヒットの多数決 (pick_consensus_jan) で抑える。

本モジュールは HTTP と判定だけを持ち、Amazon 側の照合・ジャンルゲートは
resolve_ranking_asins.resolve_yahoo_jan が行う。
"""
from __future__ import annotations

import collections
import re

import requests

from fetch_cross_search import extract_search_keyword
from fetch_rakuten import _ean_checksum_ok

YAHOO_ITEM_SEARCH_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
# Yahoo itemSearch はアプリ ID あたり 1 分 30 リクエスト。2 秒あけて余裕を持たせる。
YAHOO_DEFAULT_SLEEP = 2.1

# 楽天のリスティング題名に付く販促ブロック・文言。extract_search_keyword は Amazon の
# 題名向けなので、これらが残ると検索語が「9月26日発売 BOX レビュー特典付き」の
# ようになる (2026-10-05 の 24 件で確認)。
# 『』「」は作品名・商品名 (「すみっコぐらし」) を囲むことが多いので中身は残す。
_PROMO_BLOCK_RE = re.compile(r"[＼\\][^／/]*[／/]|[【\[][^】\]]*[】\]]")
_PROMO_PHRASE_RE = re.compile(
    # 10/4 20:00〜 のような時刻つきの日付だけを消す。単独の 1/64 は縮尺表記なので残す。
    r"\d{1,2}月\d{1,2}日(?:発売|頃|以降)?"
    r"|\d{1,2}/\d{1,2}\s*(?:[（(].[)）])?\s*(?:\d{1,2}:\d{2}|\d{1,2}時)\s*[~〜～-]?"
    r"|\d{4}年\d{1,2}月(?:上旬|中旬|下旬|\d{1,2}日)?(?:発売|再入荷)?"
    r"|レビュー(?:特典付き|投稿で[^\s]*)|送料無料|ポイント\d+倍|P\d+倍|\d+%\s*OFF|\d+円OFF"
    r"|クーポン[^\s]*|楽天[^\s]*\d+位|全品[^\s]*|予約(?:販売|商品)?|新品未開封|シュリンク付き"
    r"|正規品|即納|代引決済不可|初回生産限定|マラソン[^\s]*|あす楽",
)
# 型番らしいトークン (OP-17 / DM26-EX4 / 21371)。年 (2026) は除く。
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,4}\d{1,4}-[A-Za-z0-9]{1,6}|[A-Za-z]{2,4}-\d{1,4}|\d{5,6})(?![A-Za-z0-9])")


def build_query(title: str) -> str:
    """楽天の題名から Yahoo 検索語を作る。販促を落としてから extract_search_keyword に通し、
    そこで落ちた型番 (LEGO 21371 など) を末尾に補う。"""
    if not title:
        return ""
    clean = _PROMO_BLOCK_RE.sub(" ", title)
    clean = _PROMO_PHRASE_RE.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    query = extract_search_keyword(clean) or clean[:60]
    for code in _CODE_RE.findall(clean):
        if code.upper() not in query.upper():
            query = f"{query} {code}"
            break
    return query.strip()


def _valid_jan(code) -> str:
    s = re.sub(r"\D", "", str(code or ""))
    if len(s) == 13 and _ean_checksum_ok(s):
        return s
    # JAN-8 も EAN-13 と同じ式で検証できる (先頭を 0 で埋めて右寄せ)
    if len(s) == 8 and _ean_checksum_ok("00000" + s):
        return s
    return ""


def pick_consensus_jan(hits: list, min_votes: int = 2) -> dict:
    """上位ヒットの janCode を数え、単独首位かつ min_votes 票以上の JAN だけを返す。

    Yahoo のヒットは出品者ごとに並ぶので、同じ商品なら同じ JAN が複数並ぶ。
    1 票しかない JAN や同票首位は「別商品が混ざっている」とみなして採らない。
    """
    votes = collections.Counter()
    for h in hits or []:
        jan = _valid_jan((h or {}).get("janCode"))
        if jan:
            votes[jan] += 1
    ranked = votes.most_common(2)
    result = {"votes": dict(votes.most_common(5)), "jan": "", "reason": ""}
    if not ranked:
        result["reason"] = "no_jan_in_hits"
    elif ranked[0][1] < min_votes:
        result["reason"] = "too_few_votes"
    elif len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        result["reason"] = "tied"
    else:
        result["jan"] = ranked[0][0]
    return result


def yahoo_search_hits(query: str, client_id: str, results: int = 20,
                      timeout: float = 15.0, session=None) -> list:
    """Yahoo itemSearch の hits を返す。失敗時は例外を投げる (呼び出し側で記録する)。"""
    http = session or requests
    resp = http.get(
        YAHOO_ITEM_SEARCH_URL,
        params={"appid": client_id, "query": query, "results": results},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    hits = data.get("hits") or []
    return hits if isinstance(hits, list) else []
