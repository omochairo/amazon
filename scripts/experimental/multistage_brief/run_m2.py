"""#4841 M2「素材側の多段化」の CLI。

問い: 文章を多段化するのではなく、素材から「その商品固有の事実カード」を
多段で作って渡すと、情報利得は上がるか。

群 A (対照、T3/M1-a と同じ): 素材から narrative を1パスで書く。3 seed
(noise_floor.DEFAULT_SEEDS) で回し平均を取る (#4841 M2「群Aは3 seedの平均」。
M1-a の3回ぶんを流用してよいとされているが、その生成物はリポジトリ外
(~/multistage_runs) にあり前回実行分を機械的に再利用する経路が無いため、
本スクリプトでは同じ10 ASIN・同じプロンプト・同じ3 seedで取り直す)。
群 D: A と同じ素材 + 事実カード (fact_cards.py)。プロンプトの差は
事実カードの節の有無だけ (narrative_stage.generate_narrative_with_fact_cards)。

判定 (「M2 判定基準の差し替え」issue コメントで固定し直された条件をそのまま使う):
ASIN ごとに「D − Aの平均」を取り、固有かつ裏付けありの文の「数」と「率」の
両方で、対の差の平均のブートストラップ95%信頼区間 (10,000回・固定seed) が
0を含まない (正の側)。かつ、裏付けの無い文の数の信頼区間の下限が0を超えていない。

追加要件 (「M1 完了」issue コメント): 裏付けの無い文を「修辞・時点依存」と
「事実の主張」に分けた件数も出す (unsupported_classification.py)。判定基準
そのものは変えない。

打ち切り条件:
  - 事実カードが3件未満の ASIN が4/10以上 → 「素材が薄くてカードが作れない」
    を結論として報告し、群A/群Dは回さない
  - 1 ASIN あたり (事実カード生成+群A 3seed+群D) が15分超 → 5件で止めて報告

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
from scripts.experimental.multistage_brief import corpus, evaluation, fact_cards, narrative_stage, noise_floor, raw_material, sentence_metrics, unsupported_classification
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
logger = logging.getLogger("multistage_brief.run_m2")

DEFAULT_RUN_DIR = pathlib.Path.home() / "multistage_runs"
DEFAULT_RESULTS_OUT = "docs/multistage-generation-eval/m2_results.json"

THIN_CARD_THRESHOLD = 4  # 事実カード3件未満のASINがこの件数以上なら打ち切り
PER_ASIN_TIME_BUDGET_S = 15 * 60
TIME_CUTOFF_CHECKPOINT = 5
BOOTSTRAP_SEED = 20260914
BOOTSTRAP_N_RESAMPLES = 10_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


# --------------------------------------------------------------------------
# Phase 1: 事実カード生成 (打ち切り判定に使う)
# --------------------------------------------------------------------------

def run_phase1_fact_cards(
    selected: list[dict[str, Any]],
    *,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    session: requests.Session,
) -> dict[str, Any]:
    """全 ASIN の事実カードを生成し、打ち切り判定 (thin_count>=4) を都度確認する。

    閾値に達した時点で残りの ASIN は処理しない (コスト削減。#4841 共通ルール
    §0 のコスト意識と、M2打ち切り条件「Dは回さない」の両方に沿う)。
    """
    results: list[dict[str, Any]] = []
    thin_count = 0
    cutoff_reason: str | None = None

    for asin_info in selected:
        asin, category = asin_info["asin"], asin_info["category"]
        raw = raw_material.load_raw_material(asin)
        material_text = raw_material.build_material_text(raw)
        same_pool_articles = corpus.sample_category_articles(category, asin)
        cross_pool_articles = corpus.sample_other_category_articles(category, asin)
        same_pool = sentence_metrics.build_sentence_pool(same_pool_articles)
        cross_pool = sentence_metrics.build_sentence_pool(cross_pool_articles)

        try:
            fc = fact_cards.build_fact_cards_for_asin(
                raw, same_pool, cross_pool, material_text,
                ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
            )
        except (GemmaCallError, TruncationError, ValueError) as e:
            # gemma が壊れた JSON を返すケース (#4841 M2 実行中に実測: 「候補が多い
            # ASIN で稀に発生」)。call_gemma のリトライは HTTP 層のみで、モデルが
            # 返した文字列が壊れた JSON であること自体はリトライしない。この ASIN の
            # カード抽出を失敗として記録し、残りの ASIN の処理は止めない。
            logger.error("fact card generation failed asin=%s: %s", asin, e)
            fc = {
                "atomic_item_count": 0, "extraction_candidate_count": 0, "extraction_call_meta": None,
                "support_call_meta": None, "candidates": [], "selected_cards": [], "selected_card_count": 0,
                "is_thin": True, "error": str(e),
            }
        fc["asin"] = asin
        fc["category"] = category
        fc["material_text"] = material_text
        results.append(fc)

        if fc["is_thin"]:
            thin_count += 1
        logger.info(
            "fact cards asin=%s selected=%d thin_count=%d/%d", asin, fc["selected_card_count"], thin_count,
            THIN_CARD_THRESHOLD,
        )
        if thin_count >= THIN_CARD_THRESHOLD:
            cutoff_reason = (
                f"fact card generation thin: {thin_count} ASIN(s) with <{fact_cards.MIN_CARDS_NOT_THIN} cards"
                f" out of {len(results)} processed (threshold {THIN_CARD_THRESHOLD}) — 素材が薄くてカードが作れない"
            )
            logger.error("cutoff: %s", cutoff_reason)
            break

    return {"results": results, "thin_count": thin_count, "cutoff_reason": cutoff_reason}


# --------------------------------------------------------------------------
# Phase 2: 群A (3 seed) + 群D
# --------------------------------------------------------------------------

def _article_meta(article_path: str) -> tuple[str, list[str]]:
    article = json.loads(pathlib.Path(article_path).read_text(encoding="utf-8"))
    title = article.get("title", "") if isinstance(article, dict) else ""
    tags = article.get("tags", []) if isinstance(article, dict) else []
    return title, tags


def process_asin_phase2(
    asin_info: dict[str, Any],
    fc_result: dict[str, Any],
    *,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    session: requests.Session,
    run_dir: pathlib.Path,
) -> dict[str, Any]:
    asin, category = asin_info["asin"], asin_info["category"]
    t0 = time.time()
    material_text = fc_result["material_text"]
    raw = raw_material.load_raw_material(asin)
    title, tags = _article_meta(asin_info["article_path"])

    corpus_articles = corpus.sample_category_articles(category, asin)
    corpus_narr_vectors = corpus.embed_corpus_narratives(corpus_articles, ruri_url=ruri_url, session=session)
    same_category_pool = sentence_metrics.build_sentence_pool(corpus_articles)
    cross_category_pool = sentence_metrics.build_sentence_pool(corpus.sample_other_category_articles(category, asin))
    experience_snippets = []
    exp = raw.get("experience")
    if isinstance(exp, dict) and isinstance(exp.get("snippets"), list):
        experience_snippets = exp["snippets"]
    threshold = evaluation.load_t1_threshold()

    calls: list[dict[str, Any]] = []

    # --- 群 A: 3 seed ---
    a_runs = []
    a_unsupported_sentences: list[str] = []
    for seed in noise_floor.DEFAULT_SEEDS:
        a_out = narrative_stage.generate_narrative_baseline(
            material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
        )
        calls.append({"stage": f"A_generate_seed{seed}", **a_out["call_meta"]})
        narrative_a = a_out["narrative"]

        gain = sentence_metrics.compute_information_gain(
            narrative_a, material_text, same_category_pool, cross_category_pool,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        calls.append({"stage": f"A_entailment_seed{seed}", **(gain.get("entailment_call_meta") or {})})
        uniqueness = evaluation.compute_uniqueness(narrative_a, title, tags, corpus_narr_vectors, ruri_url=ruri_url, session=session)
        usage = (
            evaluation.compute_usage_rate(narrative_a, experience_snippets, threshold, ruri_url=ruri_url, session=session)
            if threshold is not None else {"snippet_rate": None}
        )
        for row in gain["per_sentence"]:
            if row["supported"] is False:
                a_unsupported_sentences.append(row["sentence"])

        a_runs.append({
            "seed": seed, "narrative": narrative_a,
            "unique_and_supported_count": gain["unique_and_supported_count"],
            "sentence_count": gain["sentence_count"],
            "unsupported_count": gain["unsupported_count"],
            "ratio": round(gain["unique_and_supported_count"] / gain["sentence_count"], 4) if gain["sentence_count"] else None,
            "max_sim": uniqueness["max_sim"],
            "usage_snippet_rate": usage.get("snippet_rate"),
        })

    a_count_mean = _mean([r["unique_and_supported_count"] for r in a_runs])
    a_ratio_mean = _mean([r["ratio"] for r in a_runs if r["ratio"] is not None])
    a_unsupported_mean = _mean([r["unsupported_count"] for r in a_runs])
    a_max_sim_mean = _mean([r["max_sim"] for r in a_runs if r["max_sim"] is not None])
    a_usage_mean = _mean([r["usage_snippet_rate"] for r in a_runs if isinstance(r["usage_snippet_rate"], (int, float))])

    # --- 群 D: A と同じ素材 + 事実カード ---
    fact_cards_text = fact_cards.format_fact_cards(fc_result["selected_cards"])
    d_out = narrative_stage.generate_narrative_with_fact_cards(
        material_text, fact_cards_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "D_generate", **d_out["call_meta"]})
    narrative_d = d_out["narrative"]

    d_gain = sentence_metrics.compute_information_gain(
        narrative_d, material_text, same_category_pool, cross_category_pool,
        ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "D_entailment", **(d_gain.get("entailment_call_meta") or {})})
    d_uniqueness = evaluation.compute_uniqueness(narrative_d, title, tags, corpus_narr_vectors, ruri_url=ruri_url, session=session)
    d_usage = (
        evaluation.compute_usage_rate(narrative_d, experience_snippets, threshold, ruri_url=ruri_url, session=session)
        if threshold is not None else {"snippet_rate": None}
    )
    d_unsupported_sentences = [row["sentence"] for row in d_gain["per_sentence"] if row["supported"] is False]

    d_count = d_gain["unique_and_supported_count"]
    d_ratio = round(d_count / d_gain["sentence_count"], 4) if d_gain["sentence_count"] else None

    elapsed = time.time() - t0
    result = {
        "asin": asin, "category": category, "elapsed_s": round(elapsed, 1),
        "fact_card_count": fc_result["selected_card_count"],
        "a_runs": a_runs,
        "a_mean": {
            "unique_and_supported_count": a_count_mean, "ratio": a_ratio_mean,
            "unsupported_count": a_unsupported_mean, "max_sim": a_max_sim_mean, "usage_snippet_rate": a_usage_mean,
        },
        "a_unsupported_sentences": a_unsupported_sentences,
        "d": {
            "narrative": narrative_d,
            "unique_and_supported_count": d_count, "sentence_count": d_gain["sentence_count"], "ratio": d_ratio,
            "unsupported_count": d_gain["unsupported_count"], "max_sim": d_uniqueness["max_sim"],
            "usage_snippet_rate": d_usage.get("snippet_rate"),
        },
        "d_unsupported_sentences": d_unsupported_sentences,
        "diff_count": (d_count - a_count_mean) if a_count_mean is not None else None,
        "diff_ratio": (round(d_ratio - a_ratio_mean, 4) if d_ratio is not None and a_ratio_mean is not None else None),
        "diff_unsupported_count": (
            round(d_gain["unsupported_count"] - a_unsupported_mean, 4) if a_unsupported_mean is not None else None
        ),
        "calls": calls,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{asin}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("phase2 done asin=%s elapsed=%.1fs calls=%d", asin, elapsed, len(calls))
    return result


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------

def compute_verdict(pair_results: list[dict[str, Any]]) -> dict[str, Any]:
    """「M2 判定基準の差し替え」issue コメントの条件で D の有効性を判定する (pure)。"""
    count_diffs = [p["diff_count"] for p in pair_results if p["diff_count"] is not None]
    ratio_diffs = [p["diff_ratio"] for p in pair_results if p["diff_ratio"] is not None]
    unsupported_diffs = [p["diff_unsupported_count"] for p in pair_results if p["diff_unsupported_count"] is not None]

    count_ci = bootstrap_mean_ci(count_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    ratio_ci = bootstrap_mean_ci(ratio_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)
    unsupported_ci = bootstrap_mean_ci(unsupported_diffs, seed=BOOTSTRAP_SEED, n_resamples=BOOTSTRAP_N_RESAMPLES)

    count_significant = ci_excludes_zero_on_positive_side(count_ci)
    ratio_significant = ci_excludes_zero_on_positive_side(ratio_ci)
    # 「裏付けの無い文の数の信頼区間の下限が0を超えていない」= 有意な増加ではない。
    unsupported_lower = unsupported_ci.get("lower")
    unsupported_not_increased = not (isinstance(unsupported_lower, (int, float)) and unsupported_lower > 0)

    d_effective = count_significant and ratio_significant and unsupported_not_increased

    if d_effective:
        verdict = "D有効: 数・率いずれもブートストラップ95%信頼区間が0を含まず (正の側)、裏付けの無い文は有意に増えていない"
    elif not unsupported_not_increased:
        verdict = "D無効: 裏付けの無い文の数が有意に増えている (下限>0)"
    elif not (count_significant and ratio_significant):
        verdict = "D無効: 固有かつ裏付けありの文の数・率のいずれか (または両方) の信頼区間が0をまたぐ"
    else:
        verdict = "D無効"

    return {
        "count_diff_bootstrap_ci": count_ci,
        "ratio_diff_bootstrap_ci": ratio_ci,
        "unsupported_count_diff_bootstrap_ci": unsupported_ci,
        "count_significant": count_significant,
        "ratio_significant": ratio_significant,
        "unsupported_not_increased": unsupported_not_increased,
        "d_effective": d_effective,
        "verdict": verdict,
    }


def run(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    run_dir: pathlib.Path = DEFAULT_RUN_DIR,
    results_out: pathlib.Path = pathlib.Path(DEFAULT_RESULTS_OUT),
    asin_limit: int = 0,
) -> dict[str, Any]:
    session = requests.Session()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    this_run_dir = run_dir / run_id
    t0 = time.time()

    selection = select_asins()
    selected = selection["selected"]
    if asin_limit:
        selected = selected[:asin_limit]

    body_coverage = fact_cards.count_body_content_coverage()
    logger.info("body content coverage: %s", body_coverage)

    phase1 = run_phase1_fact_cards(
        selected, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
    )
    (this_run_dir).mkdir(parents=True, exist_ok=True)
    (this_run_dir / "phase1_fact_cards.json").write_text(
        json.dumps(phase1["results"], ensure_ascii=False, indent=2), encoding="utf-8",
    )
    fact_card_light = [
        {
            "asin": r["asin"], "category": r["category"], "atomic_item_count": r["atomic_item_count"],
            "extraction_candidate_count": r["extraction_candidate_count"], "selected_card_count": r["selected_card_count"],
            "is_thin": r["is_thin"], "error": r.get("error"),
        }
        for r in phase1["results"]
    ]

    if phase1["cutoff_reason"]:
        elapsed = time.time() - t0
        payload = {
            "generated_at": _now_iso(), "run_id": run_id, "model": model, "num_ctx": num_ctx,
            "elapsed_seconds": round(elapsed, 1),
            "asin_selection": {
                "candidate_count": selection["candidate_count"], "categories_covered": selection["categories_covered"],
                "selected_asins": [s["asin"] for s in selected],
            },
            "body_content_coverage": body_coverage,
            "fact_card_summary": fact_card_light,
            "cutoff_reason": phase1["cutoff_reason"],
            "d_group_run": False,
            "per_asin_run_dir": str(this_run_dir),
        }
        results_out.parent.mkdir(parents=True, exist_ok=True)
        results_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("wrote %s (cutoff=%s)", results_out, phase1["cutoff_reason"])
        return payload

    # --- Phase 2 ---
    pair_results: list[dict[str, Any]] = []
    cutoff_reason: str | None = None
    error_asins: list[str] = []
    for i, (asin_info, fc_result) in enumerate(zip(selected, phase1["results"]), start=1):
        try:
            r = process_asin_phase2(
                asin_info, fc_result, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx,
                session=session, run_dir=this_run_dir,
            )
        except (GemmaCallError, TruncationError, ValueError) as e:
            # fact_cards と同じ理由 (壊れた JSON はリトライされない)。この ASIN は
            # D-A の対に使えないため diff_* を None にし、compute_verdict のブート
            # ストラップ入力から自然に除外する (「確認済み」と偽らない)。
            logger.error("phase2 failed asin=%s: %s", asin_info["asin"], e)
            error_asins.append(asin_info["asin"])
            r = {
                "asin": asin_info["asin"], "category": asin_info["category"], "elapsed_s": None,
                "fact_card_count": fc_result.get("selected_card_count", 0), "a_runs": [],
                "a_mean": {}, "a_unsupported_sentences": [], "d": {}, "d_unsupported_sentences": [],
                "diff_count": None, "diff_ratio": None, "diff_unsupported_count": None,
                "calls": [], "error": str(e),
            }
        pair_results.append(r)

        if i == TIME_CUTOFF_CHECKPOINT:
            slow = [x for x in pair_results if (x.get("elapsed_s") or 0) > PER_ASIN_TIME_BUDGET_S]
            if slow:
                cutoff_reason = (
                    f"per-ASIN time budget ({PER_ASIN_TIME_BUDGET_S}s) exceeded within first "
                    f"{TIME_CUTOFF_CHECKPOINT} ASIN(s): {[x['asin'] for x in slow]}"
                )
                logger.error("cutoff: %s", cutoff_reason)
                break

    # --- 裏付けの無い文の分類 (追加要件) ---
    a_unsupported_all = [s for p in pair_results for s in p["a_unsupported_sentences"]]
    d_unsupported_all = [s for p in pair_results for s in p["d_unsupported_sentences"]]
    a_classification = unsupported_classification.classify_unsupported_sentences(
        a_unsupported_all, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    d_classification = unsupported_classification.classify_unsupported_sentences(
        d_unsupported_all, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    a_classification_summary = unsupported_classification.summarize_categories(a_classification["categories"])
    d_classification_summary = unsupported_classification.summarize_categories(d_classification["categories"])

    verdict = compute_verdict(pair_results)

    cost_calls = sum(len(p.get("calls", [])) for p in pair_results)
    cost_calls += sum(1 for r in phase1["results"] if r.get("extraction_call_meta"))
    cost_calls += sum(1 for r in phase1["results"] if r.get("support_call_meta"))
    cost_seconds = sum(c.get("total_duration_s", 0) or 0 for p in pair_results for c in p.get("calls", []))
    cost_seconds += sum((r.get("extraction_call_meta") or {}).get("total_duration_s", 0) or 0 for r in phase1["results"])
    cost_seconds += sum((r.get("support_call_meta") or {}).get("total_duration_s", 0) or 0 for r in phase1["results"])

    per_asin_light = [
        {
            "asin": p["asin"], "category": p["category"], "elapsed_s": p["elapsed_s"],
            "fact_card_count": p["fact_card_count"],
            "a_mean": p["a_mean"], "d": {k: v for k, v in p["d"].items() if k != "narrative"},
            "diff_count": p["diff_count"], "diff_ratio": p["diff_ratio"], "diff_unsupported_count": p["diff_unsupported_count"],
        }
        for p in pair_results
    ]

    elapsed = time.time() - t0
    payload = {
        "generated_at": _now_iso(), "run_id": run_id, "model": model, "num_ctx": num_ctx,
        "seeds": list(noise_floor.DEFAULT_SEEDS),
        "elapsed_seconds": round(elapsed, 1),
        "asin_selection": {
            "candidate_count": selection["candidate_count"], "categories_covered": selection["categories_covered"],
            "selected_asins": [s["asin"] for s in selected],
        },
        "body_content_coverage": body_coverage,
        "fact_card_summary": fact_card_light,
        "cutoff_reason": cutoff_reason,
        "d_group_run": True,
        "asin_count_processed": len(pair_results),
        "error_asins": error_asins,
        "per_asin_summary": per_asin_light,
        "verdict": verdict,
        "unsupported_classification": {
            "method": (
                "各ASIN・各群 (A=3seed分プール/D) の unsupported 文を全ASINぶんプールし、"
                "gemma に一括で rhetorical_or_time_dependent / factual_claim を判定させた "
                "(unsupported_classification.classify_unsupported_sentences)。判定はレポート用で、"
                "D有効性の判定基準そのものには使わない"
            ),
            "a": a_classification_summary,
            "d": d_classification_summary,
        },
        "cost": {
            "asin_count": len(pair_results),
            "total_gemma_calls": cost_calls,
            "calls_per_asin_mean": round(cost_calls / len(pair_results), 2) if pair_results else None,
            "total_gemma_seconds": round(cost_seconds, 1),
            "seconds_per_asin_mean": round(cost_seconds / len(pair_results), 1) if pair_results else None,
        },
        "per_asin_run_dir": str(this_run_dir),
    }

    results_out.parent.mkdir(parents=True, exist_ok=True)
    results_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (elapsed=%.1fs, d_effective=%s)", results_out, elapsed, verdict["d_effective"])
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--results-out", default=DEFAULT_RESULTS_OUT)
    ap.add_argument("--asin-limit", type=int, default=0, help="ASIN 数の上限 (0=全10件、スモーク用)")
    args = ap.parse_args()

    try:
        run(
            ollama_url=args.ollama_url, ruri_url=args.ruri_url, model=args.model, num_ctx=args.num_ctx,
            run_dir=pathlib.Path(args.run_dir), results_out=pathlib.Path(args.results_out), asin_limit=args.asin_limit,
        )
    except (GemmaCallError, TruncationError) as e:
        logger.error("run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
