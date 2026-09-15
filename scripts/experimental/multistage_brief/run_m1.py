"""#4841 M1「判定に使える物差しを作る」の CLI。

M1-a: 群Aを同じ10 ASIN・同じプロンプトでseedだけ変えて3回回し、凡庸度 (max_sim) の
      ノイズの床 (ASINごとの標準偏差、同条件2回の差の分布) を出す。
M1-b: 同じ3回分のnarrativeについて、情報利得の指標 (固有かつ裏付けありの文の数 /
      裏付けの無い文の数) を計算する。この指標自身のノイズの床もM1-aと同じ方法
      (seedだけ変えた繰り返し) で出す。
M1-c: 「素材投入前後の版がgit履歴に両方ある記事」のペアで、指標がその既知の差を
      検出できるかを検証する。対の差 (新−旧) について、数 (固有かつ裏付けあり
      の文の数) と率 (同 ÷ 総文数) の両方でブートストラップ95%信頼区間が0を
      含まない (正の側) ときだけ「物差しとして採用」(#4841 M1-c R1・R2、
      母艦レビューでの判定の固定し直し)。

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
from scripts.experimental.multistage_brief.bootstrap import bootstrap_mean_ci, ci_excludes_zero_on_positive_side
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
BOOTSTRAP_SEED = 20260914
BOOTSTRAP_N_RESAMPLES = 10_000


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
    """有効なペアから固定 seed で最大 n 件サンプリングする。n <= 0 は「全件」を意味する

    (#4841 M1-c R3: 15件のサンプルでは信頼区間が広すぎるため、有効な58件を全件処理する)。
    """
    ordered = sorted(valid_pairs, key=lambda p: p["asin"])
    if n <= 0 or len(ordered) <= n:
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
    pair_sample_size: int = 0,
    asin_limit: int = 0,
    reuse_group_a: bool = False,
) -> dict[str, Any]:
    session = requests.Session()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    this_run_dir = run_dir / run_id
    this_run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # --- ASIN 選定 (T3 と同じ10件、群Aを再利用する場合もログ用に毎回計算する。
    #     ネットワークを使わず安価なので再計算しても問題ない) ---
    selection = select_asins()
    selected = selection["selected"]
    if asin_limit:
        selected = selected[:asin_limit]

    all_sentence_records: list[dict[str, Any]] = []
    group_a_reused_from: str | None = None

    if reuse_group_a and results_out.exists():
        # M1-a/M1-b (群Aの再生成) は #4841 M1-c R1-R3 の指摘 (判定の組み立て・
        # 全件処理) と無関係で、数値も変わらない。生成には gemma 呼び出しが
        # 30回要り、無駄にコストと待ち時間を積む (実測で全体の半分弱)。
        # 既存の集計 (results_out) から再利用し、M1-c だけ処理し直す。
        logger.info("M1-a/M1-b: reusing prior group A results from %s", results_out)
        prior = json.loads(results_out.read_text(encoding="utf-8"))
        uniqueness_floor = prior["m1a_uniqueness_noise_floor"]
        info_gain_floor = prior["m1b_information_gain_noise_floor"]
        per_asin_primary = prior["m1b_per_asin_primary_seed"]
        group_a_reused_from = str(results_out)
    else:
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
        per_asin_primary = [
            {
                "asin": r["asin"], "unique_and_supported_count": r["unique_and_supported_count"],
                "unsupported_count": r["unsupported_count"], "unresolved_count": r["unresolved_count"],
                "sentence_count": r["sentence_count"],
            }
            for r in runs if r["seed"] == primary_seed
        ]

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

        old_count = old_gain["unique_and_supported_count"]
        new_count = new_gain["unique_and_supported_count"]
        old_sentences = old_gain["sentence_count"]
        new_sentences = new_gain["sentence_count"]
        old_ratio = round(old_count / old_sentences, 4) if old_sentences else None
        new_ratio = round(new_count / new_sentences, 4) if new_sentences else None

        pair_results.append({
            "asin": asin, "category": category,
            "old_date": pair["old_date"], "new_date": pair["new_date"], "generated_at": pair["generated_at"],
            "old_sentence_count": old_sentences,
            "new_sentence_count": new_sentences,
            "old_unique_and_supported_count": old_count,
            "new_unique_and_supported_count": new_count,
            "diff_count": new_count - old_count,
            "old_ratio": old_ratio,
            "new_ratio": new_ratio,
            "diff_ratio": (
                round(new_ratio - old_ratio, 4) if old_ratio is not None and new_ratio is not None else None
            ),
            "old_unsupported_count": old_gain["unsupported_count"],
            "new_unsupported_count": new_gain["unsupported_count"],
            "diff_unsupported_count": new_gain["unsupported_count"] - old_gain["unsupported_count"],
        })
    (this_run_dir / "rewrite_pair_results.json").write_text(
        json.dumps(pair_results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # --- 判定の固定し直し (母艦レビュー R1・R2、#4841 M1-c) ---
    # 「平均差 > 1件あたりのノイズの床」は比べる対象を間違えていた (床は1件の
    # ばらつき、平均差は15件超の平均のばらつき)。平均差そのものをブートストラップで
    # 直接推定し、数・率の両方で95%信頼区間が0を含まない (正の側) ときだけ採用する。
    # 1つだけ満たす場合は「文章量の差で説明できる」として不採用 (R2)。
    count_diffs = [p["diff_count"] for p in pair_results]
    ratio_diffs = [p["diff_ratio"] for p in pair_results if p["diff_ratio"] is not None]
    unsupported_diffs = [p["diff_unsupported_count"] for p in pair_results]
    improved = sum(1 for d in count_diffs if d > 0)

    count_ci = bootstrap_mean_ci(count_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    ratio_ci = bootstrap_mean_ci(ratio_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    # 裏付けの無い文の数は同じ手法で信頼区間だけ報告する (判定には使わない。
    # 修辞的な文を一律に不支持扱いしてしまう問題があるため、母艦レビューの指示どおり)。
    unsupported_ci = bootstrap_mean_ci(unsupported_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)

    count_significant = ci_excludes_zero_on_positive_side(count_ci)
    ratio_significant = ci_excludes_zero_on_positive_side(ratio_ci)
    metric_adopted = count_significant and ratio_significant

    if metric_adopted:
        verdict = "採用: 数・率いずれもブートストラップ95%信頼区間 (10,000回) が0を含まない (正の側)"
    elif count_significant and not ratio_significant:
        verdict = "不採用: 数では信頼区間が0を含まないが、率では0をまたぐ (文章量の差で説明できる可能性を排除できない)"
    elif ratio_significant and not count_significant:
        verdict = "不採用: 率では信頼区間が0を含まないが、数では0をまたぐ"
    else:
        verdict = "不採用: 数・率いずれも信頼区間が0をまたぐ"

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
        "improved_pair_count": improved,
        "count_diff_bootstrap_ci": count_ci,
        "ratio_diff_bootstrap_ci": ratio_ci,
        "unsupported_count_diff_bootstrap_ci": unsupported_ci,
        "metric_adopted": metric_adopted,
        "verdict": verdict,
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
        "m1b_per_asin_primary_seed": per_asin_primary,
        "group_a_reused_from": group_a_reused_from,
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
    ap.add_argument(
        "--pair-sample-size", type=int, default=0,
        help="M1-c で処理するペア数の上限 (0=有効な全件。#4841 M1-c R3)",
    )
    ap.add_argument("--asin-limit", type=int, default=0, help="ASIN 数の上限 (0=全10件、スモーク用)")
    ap.add_argument(
        "--reuse-group-a", action="store_true",
        help="M1-a/M1-b (群Aの再生成) を省略し、--results-out にある既存の集計から再利用する"
        " (#4841 M1-c R3: M1-cだけ全件処理し直すときのコスト削減用)",
    )
    args = ap.parse_args()

    seeds = tuple(int(s) for s in args.seeds.split(","))
    run(
        ollama_url=args.ollama_url, ruri_url=args.ruri_url, model=args.model, num_ctx=args.num_ctx,
        run_dir=pathlib.Path(args.run_dir), results_out=pathlib.Path(args.results_out),
        seeds=seeds, pair_sample_size=args.pair_sample_size, asin_limit=args.asin_limit,
        reuse_group_a=args.reuse_group_a,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
