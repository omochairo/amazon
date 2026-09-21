"""#4841 ③ 自己批判パス (group C) 単独評価。

T3 (#7310) の群B (角度前段, angle_stage.py) は事前登録した判定基準未達で無効となり
(docs/multistage-generation-eval.md)、群B+批評だった群Cの評価には進めなかった。
群Cが無効だったB上に乗っていたため、批評・書き直し (critique_stage.py) 単独の効果は
一度も判定されていない。

問い: 角度前段を挟まず、群A (対照、1パス) の出力に批評・書き直しだけを単独で
適用すると、情報利得は上がるか。

群 A (対照、3 seed): 素材から narrative を1パスで書く (M1-a と同じ)。
群 C' : 各 seed の A 出力に critique_stage を適用する。
  - critique_stage.critique_paragraphs: 各 narrative キーの「同カテゴリ既存コーパス
    との最大類似度」(corpus.per_key_max_sim) を根拠情報として渡し、凡庸と判断された
    キーだけを gemma に指摘させる
  - critique_stage.rewrite_flagged: 指摘されたキーだけを書き直す (指摘が無いseedは
    C'=A のまま、gemma を呼ばない)

指標 (M1-b と同じ sentence_metrics.compute_information_gain): 固有かつ裏付けありの
文の数 (主) / 裏付けの無い文の数 (ガードレール)。

判定 (M2 の compute_verdict と同じ枠組みをそのまま踏襲): ASIN ごとに
「C'の3 seed平均 − Aの3 seed平均」を取り、数・率の両方で対の差の平均の
ブートストラップ95%信頼区間 (10,000回・固定seed) が0を含まない (正の側)。かつ、
裏付けの無い文の数の信頼区間の下限が0を超えていない。

追加ガードレール (T3から継承): how_to_choose の ASIN 封じ込め (許可外の競合 ASIN・
商品名の混入) を A・C' それぞれで数える。判定基準そのものには使わない (レポート用)。

対象 ASIN: select_asins.select_asins() (T3/M1/M2 と同じ選定関数・同じ固定 seed)。
同一集合で比較できるよう揃えている。

打ち切り条件: 1 ASIN あたり (3 seed分のA生成+批評+書き直し+評価) が15分超 → 5件
時点で判定して打ち切る (M2と同じ)。

生成物 (narrative全文・批評結果・gemma呼び出しメタデータ全件) は ``--run-dir``
(既定: リポジトリ外) に置き、コミットしない。PR に含めるのは集計結果
(``--results-out``) だけ。

Usage:
    python -m scripts.experimental.multistage_brief.run_c1_self_critique --asin-limit 2
    python -m scripts.experimental.multistage_brief.run_c1_self_critique
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.compute_semantic_related import DEFAULT_RURI_URL
from scripts.experimental.multistage_brief import corpus, critique_stage, guardrails, narrative_stage, noise_floor, raw_material, sentence_metrics
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
logger = logging.getLogger("multistage_brief.run_c1_self_critique")

DEFAULT_RUN_DIR = pathlib.Path.home() / "multistage_runs"
DEFAULT_RESULTS_OUT = "docs/multistage-generation-eval/c1_self_critique_results.json"

BOOTSTRAP_SEED = 20260914
BOOTSTRAP_N_RESAMPLES = 10_000
PER_ASIN_TIME_BUDGET_S = 15 * 60
TIME_CUTOFF_CHECKPOINT = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _article_meta(article_path: str) -> tuple[str, list[str]]:
    article = json.loads(pathlib.Path(article_path).read_text(encoding="utf-8"))
    title = article.get("title", "") if isinstance(article, dict) else ""
    tags = article.get("tags", []) if isinstance(article, dict) else []
    return title, tags


# --------------------------------------------------------------------------
# 1 ASIN 分の実行
# --------------------------------------------------------------------------

def process_asin(
    asin_info: dict[str, Any],
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

    raw = raw_material.load_raw_material(asin)
    material_text = raw_material.build_material_text(raw)
    allowed_asins = raw_material.allowed_competitor_asins(raw)

    corpus_articles = corpus.sample_category_articles(category, asin)
    cross_articles = corpus.sample_other_category_articles(category, asin)
    same_category_pool = sentence_metrics.build_sentence_pool(corpus_articles)
    cross_category_pool = sentence_metrics.build_sentence_pool(cross_articles)

    # ASIN 封じ込めチェック用の「無関係な商品名」サンプル (run_experiment.py と同じ)。
    foreign_product_names: list[str] = []
    for a in corpus_articles:
        product = a.get("product") if isinstance(a.get("product"), dict) else {}
        for field in ("name", "name_full"):
            v = product.get(field)
            if isinstance(v, str) and v.strip():
                foreign_product_names.append(v.strip())

    calls: list[dict[str, Any]] = []
    seed_runs: list[dict[str, Any]] = []
    a_unsupported_sentences: list[str] = []
    c_unsupported_sentences: list[str] = []
    containment_violations = {"a": 0, "c": 0}

    for seed in noise_floor.DEFAULT_SEEDS:
        a_out = narrative_stage.generate_narrative_baseline(
            material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
        )
        calls.append({"stage": f"A_generate_seed{seed}", **a_out["call_meta"]})
        narrative_a = a_out["narrative"]

        gain_a = sentence_metrics.compute_information_gain(
            narrative_a, material_text, same_category_pool, cross_category_pool,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        calls.append({"stage": f"A_entailment_seed{seed}", **(gain_a.get("entailment_call_meta") or {})})

        key_scores = corpus.per_key_max_sim(narrative_a, corpus_articles, ruri_url=ruri_url, session=session)
        critique_out = critique_stage.critique_paragraphs(
            narrative_a, key_scores, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
        )
        calls.append({"stage": f"C_critique_seed{seed}", **critique_out["call_meta"]})
        rewrite_out = critique_stage.rewrite_flagged(
            material_text, narrative_a, critique_out["flagged"],
            ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
        )
        if rewrite_out["call_meta"]:
            calls.append({"stage": f"C_rewrite_seed{seed}", **rewrite_out["call_meta"]})
        narrative_c = rewrite_out["narrative"]

        gain_c = sentence_metrics.compute_information_gain(
            narrative_c, material_text, same_category_pool, cross_category_pool,
            ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
        )
        calls.append({"stage": f"C_entailment_seed{seed}", **(gain_c.get("entailment_call_meta") or {})})

        containment_a = guardrails.check_asin_containment(
            narrative_a.get("how_to_choose", ""), allowed_asins, asin, foreign_product_names=foreign_product_names,
        )
        containment_c = guardrails.check_asin_containment(
            narrative_c.get("how_to_choose", ""), allowed_asins, asin, foreign_product_names=foreign_product_names,
        )
        if not containment_a["ok"]:
            containment_violations["a"] += 1
        if not containment_c["ok"]:
            containment_violations["c"] += 1

        for row in gain_a["per_sentence"]:
            if row["supported"] is False:
                a_unsupported_sentences.append(row["sentence"])
        for row in gain_c["per_sentence"]:
            if row["supported"] is False:
                c_unsupported_sentences.append(row["sentence"])

        a_ratio = round(gain_a["unique_and_supported_count"] / gain_a["sentence_count"], 4) if gain_a["sentence_count"] else None
        c_ratio = round(gain_c["unique_and_supported_count"] / gain_c["sentence_count"], 4) if gain_c["sentence_count"] else None
        seed_runs.append({
            "seed": seed,
            "flagged_keys": [f["key"] for f in critique_out["flagged"]],
            "rewritten_keys": rewrite_out["rewritten_keys"],
            "narrative_key_count": len(narrative_a),
            "a": {
                "unique_and_supported_count": gain_a["unique_and_supported_count"],
                "sentence_count": gain_a["sentence_count"],
                "unsupported_count": gain_a["unsupported_count"],
                "ratio": a_ratio,
            },
            "c": {
                "unique_and_supported_count": gain_c["unique_and_supported_count"],
                "sentence_count": gain_c["sentence_count"],
                "unsupported_count": gain_c["unsupported_count"],
                "ratio": c_ratio,
            },
        })

    a_count_mean = _mean([r["a"]["unique_and_supported_count"] for r in seed_runs])
    a_ratio_mean = _mean([r["a"]["ratio"] for r in seed_runs if r["a"]["ratio"] is not None])
    a_unsupported_mean = _mean([r["a"]["unsupported_count"] for r in seed_runs])
    c_count_mean = _mean([r["c"]["unique_and_supported_count"] for r in seed_runs])
    c_ratio_mean = _mean([r["c"]["ratio"] for r in seed_runs if r["c"]["ratio"] is not None])
    c_unsupported_mean = _mean([r["c"]["unsupported_count"] for r in seed_runs])
    flagged_rate = _mean([
        len(r["flagged_keys"]) / r["narrative_key_count"] for r in seed_runs if r["narrative_key_count"]
    ])

    elapsed = time.time() - t0
    result = {
        "asin": asin, "category": category, "elapsed_s": round(elapsed, 1),
        "seed_runs": seed_runs,
        "a_mean": {
            "unique_and_supported_count": a_count_mean, "ratio": a_ratio_mean, "unsupported_count": a_unsupported_mean,
        },
        "c_mean": {
            "unique_and_supported_count": c_count_mean, "ratio": c_ratio_mean, "unsupported_count": c_unsupported_mean,
        },
        "flagged_rate": flagged_rate,
        "containment_violations": containment_violations,
        "a_unsupported_sentences": a_unsupported_sentences,
        "c_unsupported_sentences": c_unsupported_sentences,
        "diff_count": (c_count_mean - a_count_mean) if a_count_mean is not None and c_count_mean is not None else None,
        "diff_ratio": (
            round(c_ratio_mean - a_ratio_mean, 4) if c_ratio_mean is not None and a_ratio_mean is not None else None
        ),
        "diff_unsupported_count": (
            round(c_unsupported_mean - a_unsupported_mean, 4)
            if a_unsupported_mean is not None and c_unsupported_mean is not None else None
        ),
        "calls": calls,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{asin}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("done asin=%s elapsed=%.1fs flagged_rate=%s", asin, elapsed, flagged_rate)
    return result


# --------------------------------------------------------------------------
# 集計・判定
# --------------------------------------------------------------------------

def compute_verdict(pair_results: list[dict[str, Any]]) -> dict[str, Any]:
    """自己批判パス (C') の有効性を判定する (M2 の compute_verdict と同じ枠組み)。"""
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

    c_effective = count_significant and ratio_significant and unsupported_not_increased

    if c_effective:
        verdict = "C有効: 数・率いずれもブートストラップ95%信頼区間が0を含まず (正の側)、裏付けの無い文は有意に増えていない"
    elif not unsupported_not_increased:
        verdict = "C無効: 裏付けの無い文の数が有意に増えている (下限>0)"
    elif not (count_significant and ratio_significant):
        verdict = "C無効: 固有かつ裏付けありの文の数・率のいずれか (または両方) の信頼区間が0をまたぐ"
    else:
        verdict = "C無効"

    return {
        "count_diff_bootstrap_ci": count_ci,
        "ratio_diff_bootstrap_ci": ratio_ci,
        "unsupported_count_diff_bootstrap_ci": unsupported_ci,
        "count_significant": count_significant,
        "ratio_significant": ratio_significant,
        "unsupported_not_increased": unsupported_not_increased,
        "c_effective": c_effective,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

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

    pair_results: list[dict[str, Any]] = []
    cutoff_reason: str | None = None
    error_asins: list[str] = []
    for i, asin_info in enumerate(selected, start=1):
        try:
            r = process_asin(
                asin_info, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx,
                session=session, run_dir=this_run_dir,
            )
        except (GemmaCallError, TruncationError, ValueError) as e:
            # M2 と同じ理由 (壊れた JSON はリトライされない)。この ASIN は
            # C'-A の対に使えないため diff_* を None にし、compute_verdict の
            # ブートストラップ入力から自然に除外する (「確認済み」と偽らない)。
            logger.error("process_asin failed asin=%s: %s", asin_info["asin"], e)
            error_asins.append(asin_info["asin"])
            r = {
                "asin": asin_info["asin"], "category": asin_info["category"], "elapsed_s": None,
                "seed_runs": [], "a_mean": {}, "c_mean": {}, "flagged_rate": None,
                "containment_violations": {"a": 0, "c": 0},
                "a_unsupported_sentences": [], "c_unsupported_sentences": [],
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

    verdict = compute_verdict(pair_results)

    cost_calls = sum(len(p.get("calls", [])) for p in pair_results)
    cost_seconds = sum(c.get("total_duration_s", 0) or 0 for p in pair_results for c in p.get("calls", []))
    total_containment_violations = {
        "a": sum(p.get("containment_violations", {}).get("a", 0) for p in pair_results),
        "c": sum(p.get("containment_violations", {}).get("c", 0) for p in pair_results),
    }

    per_asin_light = [
        {
            "asin": p["asin"], "category": p["category"], "elapsed_s": p["elapsed_s"],
            "flagged_rate": p["flagged_rate"], "a_mean": p["a_mean"], "c_mean": p["c_mean"],
            "diff_count": p["diff_count"], "diff_ratio": p["diff_ratio"], "diff_unsupported_count": p["diff_unsupported_count"],
            "containment_violations": p["containment_violations"],
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
        "cutoff_reason": cutoff_reason,
        "asin_count_processed": len(pair_results),
        "error_asins": error_asins,
        "per_asin_summary": per_asin_light,
        "verdict": verdict,
        "containment_violations_total": total_containment_violations,
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
    logger.info("wrote %s (elapsed=%.1fs, c_effective=%s)", results_out, elapsed, verdict["c_effective"])
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
    sys.exit(main())
