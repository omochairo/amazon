"""#4841 M1「判定に使える物差しを作る」の CLI。

M1-a: 群Aを同じ10 ASIN・同じプロンプトでseedだけ変えて3回回し、凡庸度 (max_sim) の
      ノイズの床 (ASINごとの標準偏差、同条件2回の差の分布) を出す。
M1-b: 同じ3回分のnarrativeについて、情報利得の指標 (固有かつ裏付けありの文の数 /
      裏付けの無い文の数) を計算する。この指標自身のノイズの床もM1-aと同じ方法
      (seedだけ変えた繰り返し) で出す。
M1-c: 「素材投入前後の版がgit履歴に両方ある記事」のペアで、指標がその既知の差を
      検出できるかを検証する。投入後の版の方が指標が高く、その差がノイズの床の
      2倍を超えれば「物差しとして採用」。

生成物 (各段の生の入出力) はリポジトリ外 (--run-dir 既定: ~/multistage_runs) に置き、
コミットしない。PRに含めるのは集計結果 (--results-out) だけ。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.audit_experience_usage import load_experience_records
from scripts.compute_semantic_related import DEFAULT_RURI_URL, discover_articles
from scripts.experimental.multistage_brief import corpus, noise_floor, raw_material, sentence_metrics
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
)
from scripts.experimental.multistage_brief.rewrite_pairs import (
    evaluate_pair,
    fetch_add_log,
    fetch_file_at_commit,
    find_rewrite_candidates,
    parse_add_events,
)
from scripts.experimental.multistage_brief.select_asins import article_category, select_asins

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("multistage_brief.run_m1")

DEFAULT_RUN_DIR = pathlib.Path.home() / "multistage_runs"
DEFAULT_RESULTS_OUT = "docs/multistage-generation-eval/m1_results.json"
VALIDATION_PAIR_SAMPLE_SIZE = 15
VALIDATION_PAIR_SAMPLE_SEED = 20260914
SPOT_CHECK_SAMPLE_SIZE = 20
SPOT_CHECK_SEED = 20260914
MATERIAL_EXCERPT_LEN = 1200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# M1-b: 1 narrative ぶんの情報利得を計算 (コーパスプールの構築込み)
# --------------------------------------------------------------------------

def compute_information_gain_for_asin(
    asin: str,
    category: str,
    narrative: dict[str, str],
    material_text: str,
    *,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    session: requests.Session,
) -> dict[str, Any]:
    same_pool_articles = corpus.sample_category_articles(category, asin)
    cross_pool_articles = corpus.sample_other_category_articles(category, asin)
    same_category_pool = sentence_metrics.build_sentence_pool(same_pool_articles)
    cross_category_pool = sentence_metrics.build_sentence_pool(cross_pool_articles)
    return sentence_metrics.compute_information_gain(
        narrative, material_text, same_category_pool, cross_category_pool,
        ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
    )


# --------------------------------------------------------------------------
# M1-c: ペア探索
# --------------------------------------------------------------------------

def find_valid_rewrite_pairs(cwd: str | None = None) -> dict[str, Any]:
    """experience.json を持つ ASIN のうち、素材投入前後の版が git 履歴に

    両方あるものを探す。母数の内訳をすべて返す (#4841 M1 共通ルール §0 の
    「除外した件数とその理由をすべて出力に残す」に準じる)。
    """
    experience_records = load_experience_records()
    gen_at_by_asin = {r["asin"]: r["generated_at"] for r in experience_records if r["snippets"]}
    target_asins = set(gen_at_by_asin.keys())

    log_text = fetch_add_log(cwd=cwd)
    events = parse_add_events(log_text)
    candidates = find_rewrite_candidates(events, target_asins)

    article_paths = discover_articles(pathlib.Path("data/articles"))
    valid_pairs: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for asin, cand in sorted(candidates.items()):
        new_path = article_paths.get(asin)
        if new_path is None:
            excluded.append({"asin": asin, "reason": "no_current_article"})
            continue
        try:
            new_article = json.loads(new_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            excluded.append({"asin": asin, "reason": "current_article_unreadable"})
            continue
        old_article = fetch_file_at_commit(cand["earliest"]["sha"], cand["earliest"]["filename"], cwd=cwd)
        if old_article is None:
            excluded.append({"asin": asin, "reason": "old_article_unreadable"})
            continue
        pair = evaluate_pair(asin, old_article, new_article, gen_at_by_asin.get(asin))
        if pair is None:
            excluded.append({"asin": asin, "reason": "date_condition_not_met"})
            continue
        pair["category"] = article_category(new_article)
        valid_pairs.append(pair)

    return {
        "experience_asin_count": len(target_asins),
        "rewrite_candidate_count": len(candidates),
        "valid_pair_count": len(valid_pairs),
        "excluded": excluded,
        "valid_pairs": valid_pairs,
    }


def select_validation_pairs(
    valid_pairs: list[dict[str, Any]], n: int = VALIDATION_PAIR_SAMPLE_SIZE, seed: int = VALIDATION_PAIR_SAMPLE_SEED,
) -> list[dict[str, Any]]:
    """処理コストを抑えるため、有効なペアから固定 seed で最大 n 件サンプリングする。"""
    ordered = sorted(valid_pairs, key=lambda p: p["asin"])
    if len(ordered) <= n:
        return ordered
    rng = random.Random(seed)
    return sorted(rng.sample(ordered, n), key=lambda p: p["asin"])


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------

def entailment_spot_check_sample(
    all_records: list[dict[str, Any]], n: int = SPOT_CHECK_SAMPLE_SIZE, seed: int = SPOT_CHECK_SEED,
) -> list[dict[str, Any]]:
    """根拠判定の精度確認用に、判定済み文から無作為に n 件抜き出す (#4841 M1-c)。"""
    judged = [r for r in all_records if r.get("supported") is not None]
    if len(judged) <= n:
        return judged
    rng = random.Random(seed)
    return rng.sample(judged, n)


def run(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    run_dir: pathlib.Path = DEFAULT_RUN_DIR,
    results_out: pathlib.Path = pathlib.Path(DEFAULT_RESULTS_OUT),
    seeds: tuple[int, ...] = noise_floor.DEFAULT_SEEDS,
    pair_sample_size: int = VALIDATION_PAIR_SAMPLE_SIZE,
    asin_limit: int = 0,
) -> dict[str, Any]:
    session = requests.Session()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    this_run_dir = run_dir / run_id
    this_run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # --- ASIN 選定 (T3 と同じ10件) ---
    selection = select_asins()
    selected = selection["selected"]
    if asin_limit:
        selected = selected[:asin_limit]

    # --- M1-a: 群Aをseed違いで複数回 ---
    logger.info("M1-a: running group A x %d seeds x %d ASIN(s)", len(seeds), len(selected))
    runs = noise_floor.run_noise_floor_experiment(
        selected, seeds=seeds, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx,
        session=session,
    )
    (this_run_dir / "group_a_runs.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    uniqueness_floor = noise_floor.compute_noise_floor(runs, metric_key="max_sim")

    # --- M1-b: 同じrunsに情報利得指標を追加 ---
    logger.info("M1-b: computing information gain for %d run(s)", len(runs))
    all_sentence_records: list[dict[str, Any]] = []
    for r in runs:
        gain = compute_information_gain_for_asin(
            r["asin"], r["category"], r["narrative"], r["material_text"],
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        r["unique_and_supported_count"] = gain["unique_and_supported_count"]
        r["unsupported_count"] = gain["unsupported_count"]
        r["unresolved_count"] = gain["unresolved_count"]
        r["sentence_count"] = gain["sentence_count"]
        material_excerpt = r["material_text"][:MATERIAL_EXCERPT_LEN]
        for row in gain["per_sentence"]:
            all_sentence_records.append({
                **row, "asin": r["asin"], "seed": r["seed"], "source": "group_a",
                "material_text_excerpt": material_excerpt,
            })
    (this_run_dir / "group_a_runs_with_gain.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    info_gain_floor = noise_floor.compute_noise_floor(runs, metric_key="unique_and_supported_count")

    primary_seed = seeds[0]
    per_asin_primary = [r for r in runs if r["seed"] == primary_seed]

    # --- M1-c: リライトペアでの指標検証 ---
    logger.info("M1-c: searching for rewrite pairs")
    pair_search = find_valid_rewrite_pairs()
    validation_pairs = select_validation_pairs(pair_search["valid_pairs"], n=pair_sample_size)
    logger.info(
        "M1-c: %d valid pair(s) found, processing %d", pair_search["valid_pair_count"], len(validation_pairs),
    )

    pair_results: list[dict[str, Any]] = []
    for pair in validation_pairs:
        asin, category = pair["asin"], pair["category"]
        material_text = raw_material.build_material_text(raw_material.load_raw_material(asin))

        old_gain = compute_information_gain_for_asin(
            asin, category, pair["old_narrative"], material_text,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        new_gain = compute_information_gain_for_asin(
            asin, category, pair["new_narrative"], material_text,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        material_excerpt = material_text[:MATERIAL_EXCERPT_LEN]
        for row in old_gain["per_sentence"]:
            all_sentence_records.append({
                **row, "asin": asin, "version": "old", "source": "rewrite_pair",
                "material_text_excerpt": material_excerpt,
            })
        for row in new_gain["per_sentence"]:
            all_sentence_records.append({
                **row, "asin": asin, "version": "new", "source": "rewrite_pair",
                "material_text_excerpt": material_excerpt,
            })

        pair_results.append({
            "asin": asin, "category": category,
            "old_date": pair["old_date"], "new_date": pair["new_date"], "generated_at": pair["generated_at"],
            "old_unique_and_supported_count": old_gain["unique_and_supported_count"],
            "new_unique_and_supported_count": new_gain["unique_and_supported_count"],
            "diff": new_gain["unique_and_supported_count"] - old_gain["unique_and_supported_count"],
            "old_unsupported_count": old_gain["unsupported_count"],
            "new_unsupported_count": new_gain["unsupported_count"],
        })
    (this_run_dir / "rewrite_pair_results.json").write_text(
        json.dumps(pair_results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    floor = info_gain_floor["floor"] or 0.0
    diffs = [p["diff"] for p in pair_results]
    improved = sum(1 for d in diffs if d > 0)
    mean_diff = round(sum(diffs) / len(diffs), 4) if diffs else None
    exceeds_floor = [d for d in diffs if d > 2 * floor]
    metric_adopted = bool(diffs) and (mean_diff is not None) and (mean_diff > 2 * floor)

    validation = {
        "pair_search": {
            "experience_asin_count": pair_search["experience_asin_count"],
            "rewrite_candidate_count": pair_search["rewrite_candidate_count"],
            "valid_pair_count": pair_search["valid_pair_count"],
            "excluded_count": len(pair_search["excluded"]),
            "excluded_by_reason": {
                reason: sum(1 for e in pair_search["excluded"] if e["reason"] == reason)
                for reason in sorted({e["reason"] for e in pair_search["excluded"]})
            },
            "processed_pair_count": len(pair_results),
        },
        "pair_results": pair_results,
        "mean_diff": mean_diff,
        "improved_pair_count": improved,
        "floor": floor,
        "pairs_exceeding_2x_floor": len(exceeds_floor),
        "metric_adopted": metric_adopted,
        "verdict": (
            "採用: 投入後の版で固有かつ裏付けありの文の数が増加し、床の2倍を超えた"
            if metric_adopted else "この指標では測れない (床の2倍を超えなかった)"
        ),
    }

    spot_check = entailment_spot_check_sample(all_sentence_records)

    elapsed = time.time() - t0
    payload = {
        "generated_at": _now_iso(),
        "run_id": run_id,
        "model": model,
        "num_ctx": num_ctx,
        "seeds": list(seeds),
        "elapsed_seconds": round(elapsed, 1),
        "asin_selection": {
            "candidate_count": selection["candidate_count"],
            "categories_covered": selection["categories_covered"],
            "selected_asins": [s["asin"] for s in selected],
        },
        "m1a_uniqueness_noise_floor": uniqueness_floor,
        "m1b_information_gain_noise_floor": info_gain_floor,
        "m1b_per_asin_primary_seed": [
            {
                "asin": r["asin"], "unique_and_supported_count": r["unique_and_supported_count"],
                "unsupported_count": r["unsupported_count"], "unresolved_count": r["unresolved_count"],
                "sentence_count": r["sentence_count"],
            }
            for r in per_asin_primary
        ],
        "m1c_validation": validation,
        "entailment_spot_check_sample": spot_check,
        "per_asin_run_dir": str(this_run_dir),
    }

    results_out.parent.mkdir(parents=True, exist_ok=True)
    results_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s (elapsed=%.1fs, metric_adopted=%s)", results_out, elapsed, metric_adopted,
    )
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--results-out", default=DEFAULT_RESULTS_OUT)
    ap.add_argument("--seeds", default=",".join(str(s) for s in noise_floor.DEFAULT_SEEDS))
    ap.add_argument("--pair-sample-size", type=int, default=VALIDATION_PAIR_SAMPLE_SIZE)
    ap.add_argument("--asin-limit", type=int, default=0, help="ASIN 数の上限 (0=全10件、スモーク用)")
    args = ap.parse_args()

    seeds = tuple(int(s) for s in args.seeds.split(","))
    run(
        ollama_url=args.ollama_url, ruri_url=args.ruri_url, model=args.model, num_ctx=args.num_ctx,
        run_dir=pathlib.Path(args.run_dir), results_out=pathlib.Path(args.results_out),
        seeds=seeds, pair_sample_size=args.pair_sample_size, asin_limit=args.asin_limit,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
