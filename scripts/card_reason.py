from __future__ import annotations

import re
from typing import Any

from score_claims import LOW_TOTAL_MAX

_PREFIX = "\u304a\u3082\u3061\u3083\u30ed\u30dc\u3088\u308a\uff1a"
_WHITESPACE = re.compile(r"\s+")


def card_reason(article: dict, total_100: int | None = None) -> str | None:
    """Return a concise, article-backed reason to choose the product."""
    try:
        if not isinstance(article, dict):
            return None
        verdict = article.get("verdict")
        headline = verdict.get("headline") if isinstance(verdict, dict) else None
        if total_100 is not None and total_100 <= LOW_TOTAL_MAX:
            headline = None
        candidates = [headline]
        persona = article.get("persona_fit")
        recommended = persona.get("recommended_for") if isinstance(persona, dict) else None
        if isinstance(recommended, list) and recommended and isinstance(recommended[0], str):
            first = _WHITESPACE.sub(" ", recommended[0]).strip()
            # \u7a7a\u306e\u300c\u5411\u304f\u4eba\u300d\u306f\u4ed8\u3051\u305a\u3001\u6b21\u306e\u5019\u88dc (pros) \u306b\u56de\u3059
            if first:
                candidates.append("\u5411\u304f\u4eba\uff1a" + first)
        product = article.get("product")
        pros = product.get("pros") if isinstance(product, dict) else None
        if isinstance(pros, list) and pros:
            candidates.append(pros[0])
        for value in candidates:
            if not isinstance(value, str):
                continue
            text = _WHITESPACE.sub(" ", value).strip()
            if text.startswith(_PREFIX):
                text = text[len(_PREFIX):].strip()
            if not text:
                continue
            return text[:60] + "\u2026" if len(text) > 60 else text
        return None
    except Exception:  # malformed article input must never break a build
        return None
