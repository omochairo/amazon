"""Q0/Q1/Q2 の検索語定義と Tavily 呼び出し (#4841 V2)。

Q0 (対照): 新規 query 0。既存の third_party_sources.json をそのまま使う
Q1: "{商品キーワード} 使ってみた 感想"
Q2: "{商品キーワード}" + include_domains (domains.build_q2_include_domains)

商品キーワードは既存と同じ extract_search_keyword。本文取得・フィルタは
fetch_third_party_sources.py と同じ関数を再利用する (二重実装しない)。
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from scripts.fetch_cross_search import extract_search_keyword
from scripts.fetch_third_party_sources import (
    TAVILY_ENDPOINT,
    _filter_sources,
    _product_title,
    record_call,
)

from scripts.experimental.search_query_trial.domains import build_q2_include_domains

GROUPS = ("Q0", "Q1", "Q2")


def keyword_for_asin(asin: str, base: pathlib.Path = pathlib.Path("data/raw/per_asin")) -> str:
    title = _product_title(asin, base)
    return extract_search_keyword(title) if title else ""


def build_query(group: str, keyword: str) -> str:
    if group == "Q1":
        return f"{keyword} 使ってみた 感想"
    # Q0 は新規 query を打たないので呼ばれない。Q2 は include_domains 側で絞る
    # だけで検索語自体は既存と同じ (商品キーワードのみ)。
    return keyword


def tavily_search(
    query: str, api_key: str, *,
    base: pathlib.Path,
    num: int = 10, include_domains: list[str] | None = None,
) -> list[dict]:
    """Tavily Search API を1回呼ぶ。fetch_third_party_sources.tavily_search と同じ
    正規化 (link/title/snippet) だが include_domains を追加で渡せる。

    owner 修正1: 本番の `fetch_for_asin` と同じ理由・同じ順序で、送信直前に
    共有台帳 (`base/_tavily_usage.json`) へ 1 回ぶんを刻む。credit はレスポンスを
    待たずに消えるため、例外で抜ける経路も含めて必ず数える。
    """
    body: dict[str, Any] = {
        "query": query,
        "max_results": max(1, min(num, 20)),
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
    }
    if include_domains:
        body["include_domains"] = include_domains
    req = urllib.request.Request(
        TAVILY_ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    record_call(base)
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


def search_for_group(
    group: str, asin: str, api_key: str, *,
    base: pathlib.Path = pathlib.Path("data/raw/per_asin"),
    max_sources: int = 8,
) -> dict[str, Any]:
    """group (Q1/Q2) の Tavily 検索を1回実行し、フィルタ後の source 一覧を返す。
    Q0 はここでは呼ばない (呼び出し側が third_party_sources.json を直接読む)。"""
    if group not in ("Q1", "Q2"):
        raise ValueError(f"search_for_group は Q1/Q2 のみ対応: {group}")
    keyword = keyword_for_asin(asin, base)
    if not keyword:
        return {"asin": asin, "group": group, "query": "", "sources": [], "raw_count": 0}
    query = build_query(group, keyword)
    include_domains = build_q2_include_domains() if group == "Q2" else None
    raw = tavily_search(query, api_key, base=base, num=10, include_domains=include_domains)
    sources = _filter_sources(raw, max_sources)
    return {
        "asin": asin, "group": group, "query": query,
        "include_domains": include_domains,
        "raw_count": len(raw), "sources": sources,
    }
