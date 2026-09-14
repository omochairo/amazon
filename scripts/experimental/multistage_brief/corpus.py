"""同一カテゴリの既存記事コーパスを Ruri で埋め込むヘルパー。

角度選択 (B②) と凡庸段落の批評 (C⑤) の両方で「既存コーパスとの類似度」を
使うため、コーパスの選定・embedding テキスト組み立てをここに集約する。
本番の embed-cache named volume には触れない (毎回そのプロセス内で計算するだけ)。
"""
from __future__ import annotations

import json
import pathlib
import random
from typing import Any

import requests

from scripts.audit_experience_usage import build_paragraph_map, cosine_similarity
from scripts.audit_uniqueness import build_uniqueness_text
from scripts.compute_semantic_related import discover_articles, embed_batch_ruri
from scripts.experimental.multistage_brief.select_asins import article_category

DEFAULT_SAMPLE_SIZE = 30


def sample_category_articles(
    category: str,
    exclude_asin: str,
    *,
    articles_dir: str | pathlib.Path = "data/articles",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 20260914,
) -> list[dict[str, Any]]:
    """同じカテゴリの既存記事から最大 ``sample_size`` 件をサンプリングする。"""
    article_paths = discover_articles(pathlib.Path(articles_dir))
    matched: list[dict[str, Any]] = []
    for asin, path in article_paths.items():
        if asin == exclude_asin:
            continue
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(article, dict):
            continue
        if article_category(article) != category:
            continue
        matched.append(article)

    rng = random.Random(seed)
    rng.shuffle(matched)
    return matched[:sample_size]


def sample_other_category_articles(
    category: str,
    exclude_asin: str,
    *,
    articles_dir: str | pathlib.Path = "data/articles",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 20260914,
) -> list[dict[str, Any]]:
    """``category`` 以外のカテゴリの既存記事から最大 ``sample_size`` 件をサンプリングする

    (#4841 M1-b: 固有性判定の負の対照「別カテゴリの記事の文」のプール用)。
    """
    article_paths = discover_articles(pathlib.Path(articles_dir))
    matched: list[dict[str, Any]] = []
    for asin, path in article_paths.items():
        if asin == exclude_asin:
            continue
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(article, dict):
            continue
        if article_category(article) == category:
            continue
        matched.append(article)

    rng = random.Random(seed)
    rng.shuffle(matched)
    return matched[:sample_size]


def embed_corpus_narratives(
    articles: list[dict[str, Any]], *, ruri_url: str, session: requests.Session,
) -> list[list[float]]:
    """コーパス記事の narrative 全文 (audit_uniqueness.build_uniqueness_text) を embed する。"""
    texts = [build_uniqueness_text(a) for a in articles]
    if not texts:
        return []
    return embed_batch_ruri(texts, ruri_url, session)


def embed_texts_document(texts: list[str], *, ruri_url: str, session: requests.Session) -> list[list[float]]:
    """任意テキスト列を document kind で embed する (compute_semantic_related と同じ経路)。"""
    if not texts:
        return []
    return embed_batch_ruri(texts, ruri_url, session)


def per_key_max_sim(
    narrative: dict[str, str],
    corpus_articles: list[dict[str, Any]],
    *,
    ruri_url: str,
    session: requests.Session,
) -> dict[str, float]:
    """narrative の各キーについて、コーパス記事の同じキーとの最大コサイン類似度を出す (C⑤)。

    audit_experience_usage.build_paragraph_map でコーパス記事側もキー単位の
    テキストに分解し、同じキー同士だけを比較する (lead 同士、how_to_choose 同士、
    という比較にすることで「型自体の類似」に引きずられにくくする)。
    """
    corpus_paragraphs = [build_paragraph_map(a) for a in corpus_articles]
    keys_needed = [k for k, v in narrative.items() if v]
    if not keys_needed:
        return {}

    own_texts = [narrative[k] for k in keys_needed]
    own_vectors = embed_batch_ruri(own_texts, ruri_url, session) if own_texts else []

    result: dict[str, float] = {}
    for key, own_vec in zip(keys_needed, own_vectors):
        corpus_texts_for_key = [p[key] for p in corpus_paragraphs if p.get(key)]
        if not corpus_texts_for_key:
            result[key] = 0.0
            continue
        corpus_vectors = embed_batch_ruri(corpus_texts_for_key, ruri_url, session)
        result[key] = round(max(cosine_similarity(own_vec, cv) for cv in corpus_vectors), 4)
    return result
