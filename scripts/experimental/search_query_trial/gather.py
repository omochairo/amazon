"""URL 一覧から本文を取得し、mine_experience と同じ candidate 形式にする (#4841 V2)。

3群とも同じ手順で本文を取る (`mine_experience.gather_third_party` と同じ
HONEST_UA・timeout・検索結果ページ除外)。Q0 は third_party_sources.json 由来の
URL だけを対象にする (news.json は検索語試験の対象外なので混ぜない)。
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

import requests

from scripts.mine_experience import HONEST_UA, MAX_CANDIDATE_TEXT_LEN, REQUEST_TIMEOUT, _html_to_text
from scripts.score_per_asin_info import is_search_result_url

logger = logging.getLogger("search_query_trial.gather")

THIRD_PARTY_NAME = "third_party_sources.json"


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def q0_urls(asin: str, base: pathlib.Path = pathlib.Path("data/raw/per_asin")) -> list[str]:
    """Q0 (対照) の対象 URL: third_party_sources.json の sources のみ (news.json は含めない)。"""
    tp = _load(base / asin / THIRD_PARTY_NAME)
    urls: list[str] = []
    if isinstance(tp, dict):
        for s in tp.get("sources", []) or []:
            if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"]:
                if not is_search_result_url(s["url"]):
                    urls.append(s["url"])
    return urls


def fetch_bodies(
    urls: list[str], *, source_type: str,
    session: requests.Session | None = None,
    sleeper=time.sleep, delay_s: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    """URL を1秒1リクエストで取得し、(candidates, fetch_log) を返す。

    candidates は mine_experience.extract_snippets が読める形
    ({"text","source_type","source_url"})。fetch_log は成功/失敗の記録
    (成功URL数・失敗URL数の集計に使う)。
    """
    session = session or requests.Session()
    candidates: list[dict] = []
    log: list[dict] = []
    for url in urls:
        try:
            resp = session.get(url, headers={"User-Agent": HONEST_UA}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("search_query_trial fetch failed for %s: %s — skip", url, e)
            log.append({"url": url, "status": "fetch_failed", "error": str(e)})
            sleeper(delay_s)
            continue
        text = _html_to_text(resp.text)
        if not text:
            log.append({"url": url, "status": "empty_body"})
            sleeper(delay_s)
            continue
        candidates.append({
            "text": text[:MAX_CANDIDATE_TEXT_LEN],
            "source_type": source_type,
            "source_url": url,
        })
        log.append({"url": url, "status": "ok", "text_len": len(text)})
        sleeper(delay_s)
    return candidates, log
