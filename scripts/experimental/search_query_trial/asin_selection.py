"""V2 対象 20 ASIN の選定 (#4841 V2)。

条件 (依頼コメントより):
  - V1 の分母 (`docs/experience-source-yield/v1_results.json` の
    `denominator.tried_asins_list`) から選ぶ
  - 記事がある
  - 既存の third_party_sources.json がある
  - カテゴリを3種類以上に散らす
固定 seed で再現できる (pure function)。カテゴリを振る round-robin は
`scripts/experimental/multistage_brief/select_asins.py` と同じ形。
"""
from __future__ import annotations

import json
import pathlib
import random
from typing import Any

from scripts.compute_semantic_related import discover_articles
from scripts.experimental.multistage_brief.select_asins import article_category

DEFAULT_V1_RESULTS = pathlib.Path("docs/experience-source-yield/v1_results.json")
DEFAULT_PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_ARTICLES_DIR = pathlib.Path("data/articles")
DEFAULT_SEED = 20260916
DEFAULT_TARGET_COUNT = 20
MIN_CATEGORIES = 3
THIRD_PARTY_NAME = "third_party_sources.json"


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_v1_denominator(v1_results_path: pathlib.Path = DEFAULT_V1_RESULTS) -> list[str]:
    data = _load(v1_results_path)
    if not isinstance(data, dict):
        return []
    asins = data.get("denominator", {}).get("tried_asins_list")
    return [a for a in asins if isinstance(a, str)] if isinstance(asins, list) else []


def find_candidates(
    v1_results_path: pathlib.Path = DEFAULT_V1_RESULTS,
    per_asin_dir: pathlib.Path = DEFAULT_PER_ASIN_DIR,
    articles_dir: pathlib.Path = DEFAULT_ARTICLES_DIR,
) -> list[dict[str, Any]]:
    """V1 の分母のうち、記事と third_party_sources.json の両方がある ASIN。"""
    pool = load_v1_denominator(v1_results_path)
    article_paths = discover_articles(pathlib.Path(articles_dir))

    out: list[dict[str, Any]] = []
    for asin in pool:
        article_path = article_paths.get(asin)
        if article_path is None:
            continue
        if not (per_asin_dir / asin / THIRD_PARTY_NAME).exists():
            continue
        try:
            article = json.loads(article_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(article, dict):
            continue
        out.append({
            "asin": asin,
            "category": article_category(article),
            "article_path": str(article_path),
        })
    return out


def select_target_asins(
    candidates: list[dict[str, Any]],
    *,
    target_count: int = DEFAULT_TARGET_COUNT,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """カテゴリを散らしながら target_count 件選ぶ (round-robin, pure function)。"""
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


def select_v2_asins(
    *,
    v1_results_path: pathlib.Path = DEFAULT_V1_RESULTS,
    per_asin_dir: pathlib.Path = DEFAULT_PER_ASIN_DIR,
    articles_dir: pathlib.Path = DEFAULT_ARTICLES_DIR,
    target_count: int = DEFAULT_TARGET_COUNT,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    candidates = find_candidates(v1_results_path, per_asin_dir, articles_dir)
    selected = select_target_asins(candidates, target_count=target_count, seed=seed)
    categories_covered = sorted({c["category"] for c in selected})
    return {
        "candidate_count": len(candidates),
        "selected": selected,
        "categories_covered": categories_covered,
        "meets_min_categories": len(categories_covered) >= MIN_CATEGORIES,
        "seed": seed,
    }
