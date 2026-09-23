"""#4841 P1「生成モデルを大きくすると情報利得は動くか」の CLI。

2026-09-23 issue コメント (仕様の一次) が定めた設計:

問い: B (角度候補) も D (事実カード) も無効だった。これは「多段化が効かない」のか
「gemma4 26B-a4b が天井」なのか切り分けられていない。**素材を固定してモデルだけ
大きくしても指標は動かないはず** (#4528「品質を縛っているのは素材の薄さ」が正しければ)。

設計:
  - **生成器だけ差し替える。** 群A (1パス baseline) の生成を gemma →
    クラウド上位モデル (K8 の agy・モデルは明示ピン) に置換する。生成器の切り替えは
    ``generator_backends.GENERATORS`` (``--generator {ollama,agy}``)。素材・
    プロンプト・対象 ASIN (select_asins.select_asins())・試行数 (既定3、
    noise_floor.DEFAULT_SEEDS と同じ本数) は据え置き
  - **物差しは固定。** sentence_metrics.compute_information_gain と裏付け判定は
    gemma のまま (判定器を替えると比較にならない)
  - 判定は run_m2.compute_verdict と同一方式 (ASIN 単位の差の bootstrap 95% CI。
    数・率とも0を含まず正、かつ裏付けの無い文の数のCI下限が0を超えない)

生成器の切り分け (このスクリプトで「生成」を担う関数と「判定」を担う関数):
  - 生成 (差し替え対象): generator_backends.generate_narrative_ollama /
    generate_narrative_agy (呼び出す agy プロンプトは narrative_stage.
    BASELINE_PROMPT_TEMPLATE と一字一句同じ)
  - 判定 (gemma固定・変えない): sentence_metrics.compute_information_gain
    (裏付け判定・固有性判定)、evaluation.compute_uniqueness、corpus.*
    (比較コーパスの取得・埋め込み)

交絡 (agy の Web 検索): CLI に tool 単位の無効化フラグが無いことを ``agy --help``
で確認済み (generator_backends.py のモジュール docstring 参照)。止める手段が
無いため、出力への URL 混入数 (generator_backends.count_urls) と agy の
``num_turns`` (1=tool往復無し) を記録し、結果に残す。

呼び出し予算: agy は個人 quota。1 run あたり最大 ``--agy-call-budget``
(既定40、issue 制約) に達したら残りの ASIN を打ち切り、``cutoff_reason`` に記録する。

生成物 (各段の生の入出力) は ``--run-dir`` (既定: リポジトリ外) に置き、
コミットしない。PR に含めるのは集計結果 (``--results-out``) だけ。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import statistics
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.compute_semantic_related import DEFAULT_RURI_URL
from scripts.experimental.multistage_brief import corpus, evaluation, generator_backends, noise_floor, raw_material, sentence_metrics
from scripts.experimental.multistage_brief.bootstrap import bootstrap_mean_ci, ci_excludes_zero_on_positive_side
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    GemmaCallError,
    TruncationError,
)
from scripts.experimental.multistage_brief.select_asins import select_asins

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("multistage_brief.run_p1_generator_ceiling")

DEFAULT_RUN_DIR = pathlib.Path.home() / "multistage_runs"
DEFAULT_RESULTS_OUT = "docs/multistage-generation-eval/p1_model_ceiling_results.json"

REFERENCE_GENERATOR = "ollama"  # 判定の基準側は常に gemma (issue: 判定器はgemma固定と対の設計)
DEFAULT_TRIALS = 3
AGY_CALL_BUDGET_DEFAULT = 40  # issue 制約: agy は個人quota、1 runの呼び出しは最大40回
BOOTSTRAP_SEED = 20260914  # run_m2.compute_verdict と同じ固定seed (「同一方式」の一部)
BOOTSTRAP_N_RESAMPLES = 10_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _article_meta(article_path: str) -> tuple[str, list[str]]:
    article = json.loads(pathlib.Path(article_path).read_text(encoding="utf-8"))
    title = article.get("title", "") if isinstance(article, dict) else ""
    tags = article.get("tags", []) if isinstance(article, dict) else []
    return title, tags


class AgyCallBudget:
    """agy 呼び出し回数の予算管理 (#4841 P1 issue 制約: 1 run 最大40回)。

    「超えそうなら停止して報告」の実装: 各試行の直前に ``allow()`` を確認し、
    予算に達していたら以降の agy 呼び出しをスキップする。予算は生成の試行単位
    ではなく **実際の subprocess 起動回数** (空応答リトライ込み) で数える。
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.stopped = False

    def allow(self) -> bool:
        return not self.stopped and self.used < self.limit

    def record(self, n: int) -> None:
        self.used += max(0, n)
        if self.used >= self.limit:
            self.stopped = True


def _judge(
    narrative: dict[str, str],
    material_text: str,
    same_category_pool,
    cross_category_pool,
    *,
    title: str,
    tags: list[str],
    corpus_narr_vectors,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    session: requests.Session,
    calls: list[dict[str, Any]],
    stage: str,
) -> tuple[dict[str, Any], list[str]]:
    """判定 (gemma固定): 情報利得 + 凡庸度。生成器を問わず同じ関数で判定する。"""
    gain = sentence_metrics.compute_information_gain(
        narrative, material_text, same_category_pool, cross_category_pool,
        ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": f"{stage}_entailment", **(gain.get("entailment_call_meta") or {})})
    uniqueness = evaluation.compute_uniqueness(
        narrative, title, tags, corpus_narr_vectors, ruri_url=ruri_url, session=session,
    )
    unsupported_sentences = [row["sentence"] for row in gain["per_sentence"] if row["supported"] is False]
    metrics = {
        "unique_and_supported_count": gain["unique_and_supported_count"],
        "sentence_count": gain["sentence_count"],
        "unsupported_count": gain["unsupported_count"],
        "ratio": round(gain["unique_and_supported_count"] / gain["sentence_count"], 4) if gain["sentence_count"] else None,
        "max_sim": uniqueness["max_sim"],
    }
    return metrics, unsupported_sentences


def _mean_metrics(runs: list[dict[str, Any]]) -> dict[str, float | None]:
    return {
        "unique_and_supported_count": _mean([r["unique_and_supported_count"] for r in runs]),
        "ratio": _mean([r["ratio"] for r in runs if r["ratio"] is not None]),
        "unsupported_count": _mean([r["unsupported_count"] for r in runs]),
        "max_sim": _mean([r["max_sim"] for r in runs if r["max_sim"] is not None]),
    }


def process_asin(
    asin_info: dict[str, Any],
    *,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    generator: str,
    agy_model: str,
    agy_timeout_s: int,
    trials: int,
    session: requests.Session,
    run_dir: pathlib.Path,
    agy_budget: AgyCallBudget,
) -> dict[str, Any]:
    asin, category = asin_info["asin"], asin_info["category"]
    t0 = time.time()
    raw = raw_material.load_raw_material(asin)
    material_text = raw_material.build_material_text(raw)
    title, tags = _article_meta(asin_info["article_path"])

    corpus_articles = corpus.sample_category_articles(category, asin)
    corpus_narr_vectors = corpus.embed_corpus_narratives(corpus_articles, ruri_url=ruri_url, session=session)
    same_category_pool = sentence_metrics.build_sentence_pool(corpus_articles)
    cross_category_pool = sentence_metrics.build_sentence_pool(corpus.sample_other_category_articles(category, asin))

    calls: list[dict[str, Any]] = []

    # --- 参照側 (常に gemma/ollama。群Aの既存プロンプト・既定3 seed) ---
    ref_runs: list[dict[str, Any]] = []
    ref_unsupported_sentences: list[str] = []
    for seed in noise_floor.DEFAULT_SEEDS[:trials]:
        out = generator_backends.generate_narrative_ollama(
            material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
        )
        calls.append({"stage": f"ref_generate_seed{seed}", **out["call_meta"]})
        metrics, unsup = _judge(
            out["narrative"], material_text, same_category_pool, cross_category_pool,
            title=title, tags=tags, corpus_narr_vectors=corpus_narr_vectors,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
            calls=calls, stage=f"ref_seed{seed}",
        )
        ref_unsupported_sentences += unsup
        ref_runs.append({"seed": seed, "narrative": out["narrative"], **metrics})
    ref_mean = _mean_metrics(ref_runs)

    # --- 比較側 (--generator で差し替え可能) ---
    cmp_runs: list[dict[str, Any]] = []
    cmp_unsupported_sentences: list[str] = []
    cmp_url_counts: list[int] = []
    cmp_num_turns: list[int | None] = []
    budget_cutoff = False
    for trial_idx in range(1, trials + 1):
        if generator == "agy":
            if not agy_budget.allow():
                logger.error("agy call budget (%d) reached — asin=%s trial=%d skipped", agy_budget.limit, asin, trial_idx)
                budget_cutoff = True
                break
            out = generator_backends.generate_narrative_agy(
                material_text, model=agy_model, timeout_s=agy_timeout_s, seed=trial_idx,
            )
            agy_budget.record(out["call_meta"].get("attempts_used") or 1)
            cmp_url_counts.append(out["call_meta"].get("url_count_in_narrative") or 0)
            cmp_num_turns.append(out["call_meta"].get("num_turns"))
        else:
            seed = noise_floor.DEFAULT_SEEDS[(trial_idx - 1) % len(noise_floor.DEFAULT_SEEDS)]
            out = generator_backends.generate_narrative_ollama(
                material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
            )
        calls.append({"stage": f"cmp_generate_trial{trial_idx}", **out["call_meta"]})
        metrics, unsup = _judge(
            out["narrative"], material_text, same_category_pool, cross_category_pool,
            title=title, tags=tags, corpus_narr_vectors=corpus_narr_vectors,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
            calls=calls, stage=f"cmp_trial{trial_idx}",
        )
        cmp_unsupported_sentences += unsup
        cmp_runs.append({"trial": trial_idx, "narrative": out["narrative"], **metrics})
    cmp_mean = _mean_metrics(cmp_runs) if cmp_runs else {
        "unique_and_supported_count": None, "ratio": None, "unsupported_count": None, "max_sim": None,
    }

    elapsed = time.time() - t0
    a = ref_mean["unique_and_supported_count"]
    b = cmp_mean["unique_and_supported_count"]
    ar, br = ref_mean["ratio"], cmp_mean["ratio"]
    au, bu = ref_mean["unsupported_count"], cmp_mean["unsupported_count"]
    result = {
        "asin": asin, "category": category, "elapsed_s": round(elapsed, 1),
        "generator": generator,
        "ref_runs": ref_runs, "ref_mean": ref_mean, "ref_unsupported_sentences": ref_unsupported_sentences,
        "cmp_runs": [{k: v for k, v in r.items() if k != "narrative"} for r in cmp_runs],
        "cmp_mean": cmp_mean, "cmp_unsupported_sentences": cmp_unsupported_sentences,
        "cmp_url_count_total": sum(cmp_url_counts) if cmp_url_counts else None,
        "cmp_num_turns": cmp_num_turns if cmp_num_turns else None,
        "cmp_budget_cutoff": budget_cutoff,
        "diff_count": (b - a) if a is not None and b is not None else None,
        "diff_ratio": (round(br - ar, 4) if ar is not None and br is not None else None),
        "diff_unsupported_count": (round(bu - au, 4) if au is not None and bu is not None else None),
        "calls": calls,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{asin}.json").write_text(
        json.dumps({**result, "cmp_runs_with_narrative": cmp_runs}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("asin=%s done elapsed=%.1fs calls=%d budget_cutoff=%s", asin, elapsed, len(calls), budget_cutoff)
    return result


def compute_verdict(pair_results: list[dict[str, Any]], *, generator: str) -> dict[str, Any]:
    """run_m2.compute_verdict と同一方式 (bootstrap 95% CI) で「生成器の差し替え」の有効性を判定する (pure)。"""
    count_diffs = [p["diff_count"] for p in pair_results if p["diff_count"] is not None]
    ratio_diffs = [p["diff_ratio"] for p in pair_results if p["diff_ratio"] is not None]
    unsupported_diffs = [p["diff_unsupported_count"] for p in pair_results if p["diff_unsupported_count"] is not None]

    count_ci = bootstrap_mean_ci(count_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    ratio_ci = bootstrap_mean_ci(ratio_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    unsupported_ci = bootstrap_mean_ci(unsupported_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)

    count_significant = ci_excludes_zero_on_positive_side(count_ci)
    ratio_significant = ci_excludes_zero_on_positive_side(ratio_ci)
    unsupported_lower = unsupported_ci.get("lower")
    unsupported_not_increased = not (isinstance(unsupported_lower, (int, float)) and unsupported_lower > 0)

    effective = count_significant and ratio_significant and unsupported_not_increased

    if effective:
        verdict = (
            f"生成器差し替え({generator})有効: 数・率いずれもブートストラップ95%信頼区間が0を含まず"
            "(正の側)、裏付けの無い文は有意に増えていない"
        )
    elif not unsupported_not_increased:
        verdict = f"生成器差し替え({generator})無効: 裏付けの無い文の数が有意に増えている (下限>0)"
    elif not (count_significant and ratio_significant):
        verdict = f"生成器差し替え({generator})無効: 固有かつ裏付けありの文の数・率のいずれか (または両方) の信頼区間が0をまたぐ"
    else:
        verdict = f"生成器差し替え({generator})無効"

    return {
        "count_diff_bootstrap_ci": count_ci,
        "ratio_diff_bootstrap_ci": ratio_ci,
        "unsupported_count_diff_bootstrap_ci": unsupported_ci,
        "count_significant": count_significant,
        "ratio_significant": ratio_significant,
        "unsupported_not_increased": unsupported_not_increased,
        "effective": effective,
        "verdict": verdict,
    }


def run(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    generator: str = "agy",
    agy_model: str = generator_backends.DEFAULT_AGY_MODEL,
    agy_timeout_s: int = generator_backends.AGY_TIMEOUT_S,
    trials: int = DEFAULT_TRIALS,
    agy_call_budget: int = AGY_CALL_BUDGET_DEFAULT,
    run_dir: pathlib.Path = DEFAULT_RUN_DIR,
    results_out: pathlib.Path = pathlib.Path(DEFAULT_RESULTS_OUT),
    asin_limit: int = 0,
) -> dict[str, Any]:
    if generator not in generator_backends.GENERATORS:
        raise ValueError(f"unknown --generator: {generator} (choices: {sorted(generator_backends.GENERATORS)})")

    session = requests.Session()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    this_run_dir = run_dir / run_id
    t0 = time.time()

    selection = select_asins()
    selected = selection["selected"]
    if asin_limit:
        selected = selected[:asin_limit]

    agy_budget = AgyCallBudget(agy_call_budget)

    pair_results: list[dict[str, Any]] = []
    error_asins: list[str] = []
    budget_stopped_at: str | None = None
    for asin_info in selected:
        if generator == "agy" and not agy_budget.allow():
            budget_stopped_at = asin_info["asin"]
            logger.error("agy call budget (%d) reached before asin=%s — stopping", agy_budget.limit, asin_info["asin"])
            break
        try:
            r = process_asin(
                asin_info, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx,
                generator=generator, agy_model=agy_model, agy_timeout_s=agy_timeout_s, trials=trials,
                session=session, run_dir=this_run_dir, agy_budget=agy_budget,
            )
        except (GemmaCallError, TruncationError, ValueError, generator_backends.AgyGenerationError) as e:
            logger.error("processing failed asin=%s: %s", asin_info["asin"], e)
            error_asins.append(asin_info["asin"])
            r = {
                "asin": asin_info["asin"], "category": asin_info["category"], "elapsed_s": None,
                "generator": generator, "ref_runs": [], "ref_mean": {}, "ref_unsupported_sentences": [],
                "cmp_runs": [], "cmp_mean": {}, "cmp_unsupported_sentences": [],
                "cmp_url_count_total": None, "cmp_num_turns": None, "cmp_budget_cutoff": False,
                "diff_count": None, "diff_ratio": None, "diff_unsupported_count": None,
                "calls": [], "error": str(e),
            }
        pair_results.append(r)

    verdict = compute_verdict(pair_results, generator=generator)

    cost_calls = sum(len(p.get("calls", [])) for p in pair_results)
    cost_seconds = sum(c.get("total_duration_s", 0) or 0 for p in pair_results for c in p.get("calls", []) if isinstance(c.get("total_duration_s"), (int, float)))

    all_num_turns = [t for p in pair_results for t in (p.get("cmp_num_turns") or []) if isinstance(t, int)]
    search_contamination = {
        "method": (
            "agy応答からURLを数える (generator_backends.count_urls) + agy --output-format json の "
            "num_turns (1=tool往復無し、2以上はtool呼び出しが挟まった可能性)。CLIにWeb検索を"
            "個別に無効化するフラグは無い (agy --help で確認、generator_backends.py docstring 参照)"
        ),
        "url_count_total": sum(p.get("cmp_url_count_total") or 0 for p in pair_results if p.get("cmp_url_count_total")),
        "trials_observed": len(all_num_turns),
        "trials_with_num_turns_gt_1": sum(1 for t in all_num_turns if t > 1),
    }

    per_asin_light = [
        {
            "asin": p["asin"], "category": p["category"], "elapsed_s": p["elapsed_s"],
            "ref_mean": p["ref_mean"], "cmp_mean": p["cmp_mean"],
            "cmp_url_count_total": p.get("cmp_url_count_total"), "cmp_num_turns": p.get("cmp_num_turns"),
            "cmp_budget_cutoff": p.get("cmp_budget_cutoff"),
            "diff_count": p["diff_count"], "diff_ratio": p["diff_ratio"], "diff_unsupported_count": p["diff_unsupported_count"],
        }
        for p in pair_results
    ]

    elapsed = time.time() - t0
    payload = {
        "generated_at": _now_iso(), "run_id": run_id,
        "reference_generator": REFERENCE_GENERATOR, "reference_model": model,
        "comparison_generator": generator,
        "comparison_model": agy_model if generator == "agy" else model,
        "judge_model": model, "num_ctx": num_ctx, "trials": trials,
        "elapsed_seconds": round(elapsed, 1),
        "asin_selection": {
            "candidate_count": selection["candidate_count"], "categories_covered": selection["categories_covered"],
            "selected_asins": [s["asin"] for s in selected],
        },
        "asin_count_processed": len(pair_results),
        "error_asins": error_asins,
        "agy_call_budget": {"limit": agy_call_budget, "used": agy_budget.used, "stopped": agy_budget.stopped},
        "budget_stopped_at_asin": budget_stopped_at,
        "per_asin_summary": per_asin_light,
        "verdict": verdict,
        "search_contamination": search_contamination,
        "cost": {
            "asin_count": len(pair_results),
            "total_gemma_calls": cost_calls,
            "calls_per_asin_mean": round(cost_calls / len(pair_results), 2) if pair_results else None,
            "total_gemma_seconds": round(cost_seconds, 1),
        },
        "per_asin_run_dir": str(this_run_dir),
    }

    results_out.parent.mkdir(parents=True, exist_ok=True)
    results_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (elapsed=%.1fs, effective=%s)", results_out, elapsed, verdict["effective"])
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument("--model", default=DEFAULT_MODEL, help="判定器 (gemma) と参照側生成に使うモデル")
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--generator", choices=sorted(generator_backends.GENERATORS), default="agy", help="比較側 (群A差し替え) の生成器")
    ap.add_argument("--agy-model", default=generator_backends.DEFAULT_AGY_MODEL)
    ap.add_argument("--agy-timeout-s", type=int, default=generator_backends.AGY_TIMEOUT_S)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--agy-call-budget", type=int, default=AGY_CALL_BUDGET_DEFAULT)
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--results-out", default=DEFAULT_RESULTS_OUT)
    ap.add_argument("--asin-limit", type=int, default=0, help="ASIN 数の上限 (0=全10件、スモーク用)")
    args = ap.parse_args()

    try:
        run(
            ollama_url=args.ollama_url, ruri_url=args.ruri_url, model=args.model, num_ctx=args.num_ctx,
            generator=args.generator, agy_model=args.agy_model, agy_timeout_s=args.agy_timeout_s,
            trials=args.trials, agy_call_budget=args.agy_call_budget,
            run_dir=pathlib.Path(args.run_dir), results_out=pathlib.Path(args.results_out), asin_limit=args.asin_limit,
        )
    except (GemmaCallError, TruncationError, generator_backends.AgyGenerationError, ValueError) as e:
        logger.error("run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
