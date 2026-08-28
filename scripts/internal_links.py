"""Fetch related articles from omochairo (omcha.jp) via its WP REST API.

API endpoint (registered on omcha.jp by a Code Snippets snippet):
    GET https://omcha.jp/wp-json/iro/v2/related?keyword=<words>&count=<N>&min_score=<S>

The response shape::

    {
      "keyword": "...",
      "total": N,
      "results": [
        {"id": ..., "title": "...", "url": "...", "score": 23, "tags": [...], ...},
        ...
      ]
    }

Used by:
- ``build_post.py`` to surface related editorial content from omcha.jp
  alongside each product article (``data["omcha_related"]`` -> template).
- ``score_calculator.py`` to award ``media_exposure`` points when the top
  omcha match has a high relevance score.

``iro/v2`` scoring (Issue #6103)
--------------------------------
v1 returned an *unbounded* sum whose scale moved with the number of query
words (measured top scores on real tag keywords ranged 1..504). v2 returns a
**0..100 normalised** score, so thresholds are comparable across keywords.
The v1 client threshold of 20 maps to **12** on the v2 scale — measured on 250
real tag keywords sampled from ``data/raw/per_asin/*/omcha_related.json``:

======================  ==========  ==========
metric                  v1 @ 20     v2 @ 12
======================  ==========  ==========
items kept per keyword  6.28        6.39
keywords with >=3 cards 70%         71%
keywords with 0 cards   12%         12%
======================  ==========  ==========

v2 also filters server-side (``min_score``), so the old ``count * 2``
over-fetch that guarded against client-side starvation is no longer needed.

Rollback: set ``OMCHA_API_BASE=https://omcha.jp/wp-json/iro/v1`` (the v1
snippet is still active) **and** restore ``min_score=20`` at the call sites —
the two are one unit, a v2 threshold against a v1 server is meaningless.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("internal_links")

DEFAULT_BASE = "https://omcha.jp/wp-json/iro/v2"
DEFAULT_TIMEOUT = 10

# 0..100 の v2 正規化スコアに対する既定の足切り。v1 の 20 と同じ通過率になる
# 点として実測で選んだ (モジュール docstring の表)。
DEFAULT_MIN_SCORE = 12


def get_related_articles(
    keyword: str,
    count: int = 3,
    min_score: int = DEFAULT_MIN_SCORE,
    base_url: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return up to ``count`` omcha.jp posts matching ``keyword``.

    ``min_score`` is a **v2-scale (0..100)** threshold and is sent to the
    server, which does the filtering. The client-side check below is kept as a
    contract guarantee (it also covers a v1 base set via ``OMCHA_API_BASE``);
    against v2 it is a no-op. Returns ``[]`` on any failure so callers can
    treat absence as "no related content" without try/except.

    Honors two env vars:
    - ``OMCHA_API_BASE`` overrides the API base URL (default omcha.jp v2).
    - ``OMCHA_API_KEY`` adds an ``api_key`` query param when the WP snippet
      has key auth enabled (empty string on the WP side = public, no key).
    """
    if not keyword or not keyword.strip():
        return []
    base = base_url or os.environ.get("OMCHA_API_BASE", DEFAULT_BASE)
    api_key = os.environ.get("OMCHA_API_KEY", "")
    # v2 filters server-side, so ask for exactly what we need. (v1 needed
    # count*2 because the threshold was applied here, after the response.)
    params: dict[str, Any] = {
        "keyword": keyword,
        "count": count,
        "min_score": min_score,
    }
    if api_key:
        params["api_key"] = api_key
    url = f"{base.rstrip('/')}/related"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.warning("omcha related fetch failed: keyword=%r err=%s", keyword, e)
        return []
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        return []
    out: list[dict[str, Any]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        score = r.get("score")
        if not isinstance(score, (int, float)) or score < min_score:
            continue
        title = r.get("title")
        url_ = r.get("url")
        if not title or not url_:
            continue
        thumb = r.get("thumbnail")
        thumb = str(thumb) if isinstance(thumb, str) and thumb else None
        out.append({
            "title": str(title),
            "url": str(url_),
            "score": int(score),
            "thumbnail": thumb,
        })
        if len(out) >= count:
            break
    return out


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    kw = " ".join(sys.argv[1:]) or "知育玩具"
    res = get_related_articles(kw)
    print(json.dumps(res, ensure_ascii=False, indent=2))
