"""評価 3種 (#4841 T3 評価表): 凡庸度 / 素材の使用率。ガードレール2種は guardrails.py。"""
from __future__ import annotations

import json
import math
import pathlib
from typing import Any

import requests

from scripts.audit_experience_usage import cosine_similarity
from scripts.audit_uniqueness import build_uniqueness_text
from scripts.compute_semantic_related import embed_batch_ruri

DEFAULT_T1_RESULT_PATH = "data/analytics/experience_usage.json"


def load_t1_threshold(path: str | pathlib.Path = DEFAULT_T1_RESULT_PATH) -> float | None:
    """T1 (#4841) が較正した使用判定の閾値を読む。T3 はこの絶対値をそのまま使う。

    (#4841 T3 追記コメント: 「素材の使用率」は T1 の閾値と同一 ASIN 対照との差で読む)
    """
    p = pathlib.Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    threshold = data.get("threshold")
    if isinstance(threshold, dict):
        threshold = threshold.get("value")
    return float(threshold) if isinstance(threshold, (int, float)) else None


def compute_usage_rate(
    narrative: dict[str, str],
    experience_snippets: list[dict[str, Any]],
    threshold: float,
    *,
    ruri_url: str,
    session: requests.Session,
) -> dict[str, Any]:
    """T1 と同じ方式: snippet (query) と段落 (document) の最大コサイン類似度で使用判定。"""
    paragraph_texts = [t for t in narrative.values() if t]
    if not paragraph_texts or not experience_snippets:
        return {"snippet_rate": None, "article_used": False, "per_snippet": []}

    paragraph_vectors = embed_batch_ruri(paragraph_texts, ruri_url, session)
    snippet_texts = [s.get("text", "") for s in experience_snippets if isinstance(s, dict) and s.get("text")]
    if not snippet_texts:
        return {"snippet_rate": None, "article_used": False, "per_snippet": []}
    snippet_vectors = embed_batch_ruri(snippet_texts, ruri_url, session)

    per_snippet = []
    used_count = 0
    for text, vec in zip(snippet_texts, snippet_vectors):
        max_sim = max(cosine_similarity(vec, pv) for pv in paragraph_vectors)
        used = max_sim >= threshold
        used_count += int(used)
        per_snippet.append({"text": text, "max_sim": round(max_sim, 4), "used": used})

    return {
        "snippet_rate": round(used_count / len(snippet_texts), 4),
        "article_used": used_count > 0,
        "per_snippet": per_snippet,
    }


def compute_uniqueness(
    narrative: dict[str, str],
    title: str,
    tags: list[str],
    corpus_vectors: list[list[float]],
    *,
    ruri_url: str,
    session: requests.Session,
) -> dict[str, Any]:
    """audit_uniqueness.py と同じ text builder で max_sim / centroid_sim を出す。"""
    pseudo_article = {"title": title, "narrative": narrative, "tags": tags}
    text = build_uniqueness_text(pseudo_article)
    own_vec = embed_batch_ruri([text], ruri_url, session)[0]

    if not corpus_vectors:
        return {"max_sim": None, "centroid_sim": None}

    max_sim = max(cosine_similarity(own_vec, cv) for cv in corpus_vectors)

    normed = []
    for v in corpus_vectors:
        norm = math.sqrt(sum(x * x for x in v))
        normed.append([x / norm for x in v] if norm else v)
    dim = len(normed[0]) if normed else 0
    centroid = [sum(v[i] for v in normed) / len(normed) for i in range(dim)] if dim else []
    centroid_sim = cosine_similarity(own_vec, centroid) if centroid else None

    return {
        "max_sim": round(max_sim, 4),
        "centroid_sim": round(centroid_sim, 4) if centroid_sim is not None else None,
    }
