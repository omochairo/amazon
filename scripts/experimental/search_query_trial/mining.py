"""gemma 抽出 (#4841 V2)。mine_experience の抽出プロンプト/usable_as 割当を
そのまま使うが、呼び出しは `ollama_client.call_gemma` 経由にして num_ctx 明示・
切り詰め検出 (共通ルール) を効かせる。mine_experience.extract_snippets 自身は
この検出を持たないため、実験側でラップする。
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from scripts.mine_experience import EXTRACTION_PROMPT_TEMPLATE, _USABLE_AS_MAP

from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    GemmaCallError,
    TruncationError,
    call_gemma,
    parse_json_response,
)

logger = logging.getLogger("search_query_trial.mining")


def extract_snippets_checked(
    candidate: dict[str, Any], product_name: str, brand: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """1 candidate 分を gemma に投げる。切り詰め・失敗は snippet 0 件 + call meta
    の "failed"/"truncated" として返す (黙って続行しない。#4841 共通要件)。"""
    text = candidate.get("text", "")
    if not text.strip():
        return [], {"status": "empty_text"}

    prompt = EXTRACTION_PROMPT_TEMPLATE.format(product_name=product_name, brand=brand, text=text)
    try:
        call = call_gemma(
            prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx,
            temperature=0, session=session, format_json=True,
        )
    except TruncationError as e:
        logger.error("extraction call truncated for %s: %s", candidate.get("source_url"), e)
        return [], {"status": "truncated", "reason": str(e)}
    except GemmaCallError as e:
        logger.warning("extraction call failed for %s: %s", candidate.get("source_url"), e)
        return [], {"status": "failed", "reason": str(e)}

    try:
        parsed = parse_json_response(call["text"])
    except ValueError as e:
        logger.warning("extraction response not JSON for %s: %s", candidate.get("source_url"), e)
        return [], {"status": "bad_json", "reason": str(e), "call": call}

    meta = {"status": "ok", "call": call}
    if not isinstance(parsed, dict) or not parsed.get("entailed"):
        return [], meta
    raw_snippets = parsed.get("snippets")
    if not isinstance(raw_snippets, list):
        return [], meta

    usable_as = _USABLE_AS_MAP.get(candidate.get("source_type", ""), "quote")
    out: list[dict] = []
    for s in raw_snippets:
        if not isinstance(s, dict):
            continue
        aspect = s.get("aspect")
        text_out = s.get("text")
        if not isinstance(aspect, str) or not isinstance(text_out, str) or not text_out.strip():
            continue
        out.append({
            "aspect": aspect,
            "text": text_out.strip(),
            "source_type": candidate.get("source_type", ""),
            "source_url": candidate.get("source_url", ""),
            "usable_as": usable_as,
            "confidence": s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "medium",
        })
    return out, meta
