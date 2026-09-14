"""#4841 T3 の対象 ASIN (10件) 選定。

条件 (設計要求どおり):
  - experience.json の snippet が3件以上で、`不満` を1件以上含む
  - カテゴリ (product.edu_domains の先頭要素) を3種類以上に散らす
  - 既存の post_v7 記事がある (参考比較用)
選定は固定 seed で再現できる (pure function、乱数以外の外部状態に依存しない)。
"""
from __future__ import annotations

import glob
import json
import pathlib
import random
from typing import Any

from scripts.compute_semantic_related import discover_articles
from scripts.quality_gate import HOW_TO_CHOOSE_ENFORCE_FROM

DEFAULT_EXPERIENCE_GLOB = "data/raw/per_asin/*/experience.json"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_SEED = 20260914
DEFAULT_TARGET_COUNT = 10
MIN_SNIPPETS = 3
REQUIRED_ASPECT = "不満"
MIN_CATEGORIES = 3


def article_category(article: dict[str, Any]) -> str:
    """product.edu_domains の先頭要素をカテゴリとして使う (STEM/言語/運動/想像)。"""
    product = article.get("product")
    domains = product.get("edu_domains") if isinstance(product, dict) else None
    if isinstance(domains, list) and domains:
        return str(domains[0])
    return "unknown"


def is_post_v7(article: dict[str, Any]) -> bool:
    """slug 先頭10文字 (YYYY-MM-DD) が v7 施行日以降か (audit_uniqueness.py と同じ判定)。

    slug が無い/短い場合は安全側で post_v7 扱いにする (audit_uniqueness.py の
    _iso_week_label 前段の cohort 分類と同じ方針)。
    """
    slug = article.get("slug")
    if not isinstance(slug, str) or len(slug) < 10:
        return True
    return slug[:10] >= HOW_TO_CHOOSE_ENFORCE_FROM


def find_candidates(
    experience_glob: str = DEFAULT_EXPERIENCE_GLOB,
    articles_dir: str | pathlib.Path = DEFAULT_ARTICLES_DIR,
) -> list[dict[str, Any]]:
    """条件を満たす候補 ASIN の一覧を作る (snippet 数・不満有無・カテゴリ・post_v7)。"""
    article_paths = discover_articles(pathlib.Path(articles_dir))
    out: list[dict[str, Any]] = []

    for exp_path in sorted(glob.glob(experience_glob)):
        asin = pathlib.Path(exp_path).parent.name
        try:
            exp = json.loads(pathlib.Path(exp_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(exp, dict):
            continue
        snippets = exp.get("snippets")
        snippets = snippets if isinstance(snippets, list) else []
        if len(snippets) < MIN_SNIPPETS:
            continue
        aspects = {s.get("aspect") for s in snippets if isinstance(s, dict)}
        if REQUIRED_ASPECT not in aspects:
            continue

        article_path = article_paths.get(asin)
        if article_path is None:
            continue
        try:
            article = json.loads(article_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(article, dict):
            continue
        if not is_post_v7(article):
            continue

        out.append({
            "asin": asin,
            "snippet_count": len(snippets),
            "category": article_category(article),
            "article_path": str(article_path),
        })

    return out


def select_target_asins(
    candidates: list[dict[str, Any]],
    *,
    target_count: int = DEFAULT_TARGET_COUNT,
    min_categories: int = MIN_CATEGORIES,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """固定 seed でカテゴリを散らしながら ``target_count`` 件選ぶ (pure function)。

    各カテゴリのバケツをシャッフルしてから round-robin で取り出すことで、
    候補が十分にあれば自然と複数カテゴリに散らばる。
    """
    rng = random.Random(seed)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_category.setdefault(c["category"], []).append(c)
    for bucket in by_category.values():
        rng.shuffle(bucket)

    categories = sorted(by_category.keys())
    rng.shuffle(categories)

    selected: list[dict[str, Any]] = []
    while len(selected) < target_count:
        progressed = False
        for cat in categories:
            if len(selected) >= target_count:
                break
            bucket = by_category[cat]
            if bucket:
                selected.append(bucket.pop())
                progressed = True
        if not progressed:
            break

    return selected


def select_asins(
    *,
    experience_glob: str = DEFAULT_EXPERIENCE_GLOB,
    articles_dir: str | pathlib.Path = DEFAULT_ARTICLES_DIR,
    target_count: int = DEFAULT_TARGET_COUNT,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """候補抽出 + 選定をまとめて実行し、選定理由も含めて返す (CLI/レポート用)。"""
    candidates = find_candidates(experience_glob, articles_dir)
    selected = select_target_asins(candidates, target_count=target_count, seed=seed)
    categories_covered = sorted({c["category"] for c in selected})
    return {
        "candidate_count": len(candidates),
        "selected": selected,
        "categories_covered": categories_covered,
        "seed": seed,
    }
