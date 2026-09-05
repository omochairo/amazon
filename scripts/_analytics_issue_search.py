"""_analytics_issue_search.py

A レーン opener 群 (A-1〜A-5 / A-7, epic #1356) が重複起票を防ぐために引く
`search/issues` の共通実装。

## なぜ共通化したか

6 本の opener が同じ検索をそれぞれ手書きしていて、**全部 1 ページ (100 件) 打ち切り**
だった。マーカー付きの open Issue が 100 件を超えると、溢れたぶんは「存在しない」と
判定される = **重複した Issue を立てる**。件数が増えてから静かに壊れる型。

## ページング

`page` を進めて、返りが `per_page` 未満になるまで読む。GitHub Search API は
**合計 1,000 件 (10 ページ) が上限**でそれ以上は取れないので、`MAX_PAGES` で止めて
警告を出す。ここは黙って打ち切らない — 上限に当たったなら重複起票のリスクが実際に
あるので、log に残して気づけるようにする。

## 並び順は用途で逆になる

- **opener (ここ)**: `created desc`。1,000 件上限で溢れるなら、落とすのは古い側。
  今週の検出とぶつかりやすいのは直近に立った Issue なので、新しい側を必ず見る
- **closer** (`close_expired_analytics_issues.py`): `created asc`。溢れるのは新しい側
  = まだ期限内のもの。期限切れを取りこぼさない側に倒す

取りこぼしの意味が逆 (opener は重複起票、closer は掃除漏れ) なので、順序も逆になる。

## 索引ラグへの再試行

GitHub Search API は書き込み直後の反映に数秒〜数十秒のラグがある (2026-07-14、
amazon-home-ops の answerability-audit workflow 実運用で観測)。1 ページ目が 0 件の
ときだけ短い間隔で再試行する。真に 0 件なら再試行しても 0 のまま受け入れる。

なお同一 run 内で closer が閉じたぶんの取り扱いは別経路 (`_analytics_closed_keys`)。
あちらは「閉じたのに open と返る」ラグ、こちらは「在るのに 0 件と返る」ラグで、
必要な補正が逆向きなので分けてある。
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger("analytics_issue_search")

PER_PAGE = 100
# GitHub Search API は合計 1,000 件が上限 (100 件 × 10 ページ)
MAX_PAGES = 10
SEARCH_MAX_ATTEMPTS = 3
SEARCH_RETRY_SLEEP_SECONDS = 3.0


def _run_search(query: str, page: int, order: str) -> list[dict]:
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", f"per_page={PER_PAGE}", "-f", f"page={page}",
         "-f", "sort=created", "-f", f"order={order}"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(res.stdout).get("items", [])


def search_issues(query: str, *, order: str = "desc", sleeper=time.sleep) -> list[dict]:
    """検索結果を全ページ集める (Search API の上限 1,000 件まで)。"""
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        got = _run_search(query, page, order)
        if page == 1 and not got:
            # 索引ラグ対策。1 ページ目が 0 件のときだけ短く粘る
            for attempt in range(2, SEARCH_MAX_ATTEMPTS + 1):
                logger.info("search/issues returned 0 items (attempt %d/%d); retrying "
                            "in case of search index lag", attempt - 1, SEARCH_MAX_ATTEMPTS)
                sleeper(SEARCH_RETRY_SLEEP_SECONDS)
                got = _run_search(query, page, order)
                if got:
                    break
        items.extend(got)
        if len(got) < PER_PAGE:
            return items
    logger.warning(
        "search/issues hit the %d-page cap (%d items) for %r — results beyond the "
        "Search API's 1,000-item limit are invisible, so duplicate Issues are possible",
        MAX_PAGES, len(items), query,
    )
    return items


def extract_marked_keys(items: list[dict], marker_prefix: str) -> set[str]:
    """検索結果の本文から `<!-- <prefix><キー> -->` のキーを回収する。"""
    taken: set[str] = set()
    for it in items:
        body = it.get("body") or ""
        idx = body.find(marker_prefix)
        while idx >= 0:
            tail = body[idx + len(marker_prefix):]
            key = tail.split("-->", 1)[0].strip()
            if key:
                taken.add(key)
            idx = body.find(marker_prefix, idx + 1)
    return taken


def find_taken_keys(repo: str, marker_prefix: str, *, sleeper=time.sleep) -> set[str]:
    """label=quality,analytics の open Issue から重複防止キーを集める。"""
    query = (
        f"repo:{repo} is:issue is:open label:quality label:analytics "
        f'in:body "{marker_prefix}"'
    )
    return extract_marked_keys(search_issues(query, sleeper=sleeper), marker_prefix)
