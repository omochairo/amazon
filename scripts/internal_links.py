"""Fetch related articles from omochairo (omcha.jp) via its WP REST API.

API endpoint (registered on omcha.jp by an WPCode snippet):
    GET https://omcha.jp/wp-json/iro/v1/related?keyword=<words>&count=<N>

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
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("internal_links")

DEFAULT_BASE = "https://omcha.jp/wp-json/iro/v1"
DEFAULT_TIMEOUT = 10


def get_related_articles(
    keyword: str,
    count: int = 3,
    min_score: int = 10,
    base_url: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return up to ``count`` omcha.jp posts matching ``keyword``.

    Filters out entries whose omcha-side score is below ``min_score`` so we
    don't surface very weak matches. Returns ``[]`` on any failure so callers
    can treat absence as "no related content" without try/except.

    Honors two env vars:
    - ``OMCHA_API_BASE`` overrides the API base URL (default omcha.jp).
    - ``OMCHA_API_KEY`` adds an ``api_key`` query param when the WP snippet
      has key auth enabled (empty string on the WP side = public, no key).
    """
    if not keyword or not keyword.strip():
        return []
    base = base_url or os.environ.get("OMCHA_API_BASE", DEFAULT_BASE)
    api_key = os.environ.get("OMCHA_API_KEY", "")
    # Ask for more than we need so min_score filtering doesn't starve us.
    params: dict[str, Any] = {"keyword": keyword, "count": max(count * 2, 6)}
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
        out.append({"title": str(title), "url": str(url_), "score": int(score)})
        if len(out) >= count:
            break
    return out


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    kw = " ".join(sys.argv[1:]) or "知育玩具"
    res = get_related_articles(kw)
    print(json.dumps(res, ensure_ascii=False, indent=2))
