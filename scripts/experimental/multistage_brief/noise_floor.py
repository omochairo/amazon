"""#4841 M1-a: 凡庸度のノイズの床。

T3 の群 A (対照、1パス) を同じ ASIN・同じプロンプトで seed だけ変えて複数回回し、
ASIN ごとの max_sim の標準偏差と「同じ条件の2回の差」の分布を出す。以後の
「差がある」は、この床の2倍を超えたときだけ言う (#4841 M1 設計要求)。
"""
from __future__ import annotations

import itertools
import json
import pathlib
import statistics
from typing import Any

import requests

from scripts.audit_experience_usage import percentile
from scripts.experimental.multistage_brief import corpus, evaluation, narrative_stage, raw_material
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
)

DEFAULT_SEEDS = (20260914, 20260915, 20260916)


def run_group_a_once(
    asin_info: dict[str, Any],
    *,
    seed: int,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session,
) -> dict[str, Any]:
    """1 ASIN・1 seed ぶんの群A生成 + 凡庸度 (max_sim/centroid_sim) 計測。"""
    asin = asin_info["asin"]
    category = asin_info["category"]
    raw = raw_material.load_raw_material(asin)
    material_text = raw_material.build_material_text(raw)

    article = json.loads(pathlib.Path(asin_info["article_path"]).read_text(encoding="utf-8"))
    title = article.get("title", "") if isinstance(article, dict) else ""
    tags = article.get("tags", []) if isinstance(article, dict) else []

    corpus_articles = corpus.sample_category_articles(category, asin)
    corpus_vectors = corpus.embed_corpus_narratives(corpus_articles, ruri_url=ruri_url, session=session)

    out = narrative_stage.generate_narrative_baseline(
        material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
    )
    uniqueness = evaluation.compute_uniqueness(
        out["narrative"], title, tags, corpus_vectors, ruri_url=ruri_url, session=session,
    )
    return {
        "asin": asin, "seed": seed, "category": category, "narrative": out["narrative"],
        "material_text": material_text, "max_sim": uniqueness["max_sim"], "centroid_sim": uniqueness["centroid_sim"],
        "call_meta": out["call_meta"],
    }


def run_noise_floor_experiment(
    asin_infos: list[dict[str, Any]],
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """全 ASIN x 全 seed の群A生成を実行する (IO、gemma/Ruri を叩く)。"""
    session = session or requests.Session()
    runs: list[dict[str, Any]] = []
    for asin_info in asin_infos:
        for seed in seeds:
            runs.append(run_group_a_once(
                asin_info, seed=seed, ollama_url=ollama_url, ruri_url=ruri_url, model=model,
                num_ctx=num_ctx, session=session,
            ))
    return runs


def compute_noise_floor(runs: list[dict[str, Any]], metric_key: str = "max_sim") -> dict[str, Any]:
    """ASIN ごとの標準偏差と、同条件2回の差の分布を出す (pure function)。

    runs: [{"asin", "seed", <metric_key>}, ...] (run_noise_floor_experiment の出力と同じ形)。
    metric_key を変えると、任意の数値指標 (例: M1-b の unique_and_supported_count)
    に同じ手法 (同一ASIN・同一プロンプト・seedのみ変えた繰り返し) の床を計算できる。
    """
    by_asin: dict[str, list[float]] = {}
    for r in runs:
        if isinstance(r.get(metric_key), (int, float)):
            by_asin.setdefault(r["asin"], []).append(r[metric_key])

    per_asin_stdev: dict[str, float | None] = {}
    pairwise_diffs: list[float] = []
    for asin, values in by_asin.items():
        per_asin_stdev[asin] = round(statistics.stdev(values), 4) if len(values) >= 2 else None
        for a, b in itertools.combinations(values, 2):
            pairwise_diffs.append(round(abs(a - b), 4))

    stdev_values = [v for v in per_asin_stdev.values() if v is not None]
    return {
        "asin_count": len(by_asin),
        "runs_per_asin": {a: len(v) for a, v in by_asin.items()},
        "per_asin_stdev": per_asin_stdev,
        "stdev_distribution": {
            "mean": round(statistics.mean(stdev_values), 4) if stdev_values else None,
            "p50": percentile(stdev_values, 50),
            "p90": percentile(stdev_values, 90),
        },
        "pairwise_diff_distribution": {
            "count": len(pairwise_diffs),
            "mean": round(statistics.mean(pairwise_diffs), 4) if pairwise_diffs else None,
            "p50": percentile(pairwise_diffs, 50),
            "p90": percentile(pairwise_diffs, 90),
        },
        # 「差がある」の判定に使う床。p90 (保守的、外れ値寄りの差もノイズとして
        # 許容する側) を採用し、以後は floor*2 を超えたときだけ差を主張する。
        "floor": percentile(pairwise_diffs, 90),
    }
