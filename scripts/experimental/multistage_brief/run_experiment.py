"""#4841 T3 オフライン実験の CLI。3群 (A/B/C) x 10 ASIN を実行し、
各 ASIN の生の入出力を ``--run-dir`` (既定: リポジトリ外) に書き、
集計結果だけを ``docs/multistage-generation-eval/results.json`` に残す。

本番の生成経路・named volume・data/articles には一切書き込まない。
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.compute_semantic_related import DEFAULT_RURI_URL
from scripts.experimental.multistage_brief import (
    angle_stage,
    corpus,
    critique_stage,
    evaluation,
    guardrails,
    narrative_stage,
    raw_material,
)
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    GemmaCallError,
    TruncationError,
)
from scripts.experimental.multistage_brief.select_asins import select_asins

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("multistage_brief.run_experiment")

DEFAULT_RUN_DIR = pathlib.Path.home() / "multistage_runs"
DEFAULT_RESULTS_OUT = "docs/multistage-generation-eval/results.json"

PER_ASIN_TIME_BUDGET_S = 15 * 60
BROKEN_ABORT_THRESHOLD = 3
BROKEN_ABORT_WINDOW = 10
TIME_CUTOFF_CHECKPOINT = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_broken_narrative(narrative: dict[str, str]) -> bool:
    """A群のnarrativeが記事として成立しているかの粗いチェック (打ち切り条件用)。"""
    if not narrative or len(narrative) < 5:
        return True
    texts = list(narrative.values())
    if len(set(texts)) < len(texts):
        return True
    return False


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
    asin = asin_info["asin"]
    category = asin_info["category"]
    t0 = time.time()

    raw = raw_material.load_raw_material(asin)
    material_text = raw_material.build_material_text(raw)
    allowed_asins = raw_material.allowed_competitor_asins(raw)
    article = json.loads(pathlib.Path(asin_info["article_path"]).read_text(encoding="utf-8"))
    title = article.get("title", "") if isinstance(article, dict) else ""
    tags = article.get("tags", []) if isinstance(article, dict) else []

    corpus_articles = corpus.sample_category_articles(category, asin)
    corpus_narr_vectors = corpus.embed_corpus_narratives(corpus_articles, ruri_url=ruri_url, session=session)
    # ASIN 封じ込めチェック用の「無関係な商品名」サンプル: 同カテゴリの他 ASIN 記事
    # (= 比較対象として許可されていない商品) の product.name / name_full。
    foreign_product_names: list[str] = []
    for a in corpus_articles:
        product = a.get("product") if isinstance(a.get("product"), dict) else {}
        for field in ("name", "name_full"):
            v = product.get(field)
            if isinstance(v, str) and v.strip():
                foreign_product_names.append(v.strip())

    calls: list[dict[str, Any]] = []

    # --- 群 A (対照) ---
    a_out = narrative_stage.generate_narrative_baseline(
        material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "A_generate", **a_out["call_meta"]})
    narrative_a = a_out["narrative"]

    # --- 群 B (前段あり) ---
    angle_out = angle_stage.generate_angle_candidates(
        material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "B_angle_candidates", **angle_out["call_meta"]})
    selection = angle_stage.select_angle(
        angle_out["candidates"], corpus_narr_vectors, ruri_url=ruri_url, session=session,
    )
    selected_angle = selection["selected"] or {"angle": "", "evidence": ""}
    memo_out = angle_stage.build_design_memo(
        material_text, selected_angle, allowed_asins,
        ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "B_design_memo", **memo_out["call_meta"]})
    b_out = narrative_stage.generate_narrative_with_memo(
        material_text, selected_angle.get("angle", ""), memo_out["memo"],
        ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "B_generate", **b_out["call_meta"]})
    narrative_b = b_out["narrative"]

    # --- 群 C (前段 + 後段) ---
    key_scores = corpus.per_key_max_sim(narrative_b, corpus_articles, ruri_url=ruri_url, session=session)
    critique_out = critique_stage.critique_paragraphs(
        narrative_b, key_scores, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    calls.append({"stage": "C_critique", **critique_out["call_meta"]})
    rewrite_out = critique_stage.rewrite_flagged(
        material_text, narrative_b, critique_out["flagged"],
        ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    if rewrite_out["call_meta"]:
        calls.append({"stage": "C_rewrite", **rewrite_out["call_meta"]})
    narrative_c = rewrite_out["narrative"]

    # --- ガードレール + 評価 (群ごと共通) ---
    experience_snippets = []
    exp = raw.get("experience")
    if isinstance(exp, dict) and isinstance(exp.get("snippets"), list):
        experience_snippets = exp["snippets"]
    threshold = evaluation.load_t1_threshold()

    groups_out: dict[str, Any] = {}
    for label, narrative in (("A", narrative_a), ("B", narrative_b), ("C", narrative_c)):
        entail = guardrails.run_entailment_check(
            material_text, narrative, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
        )
        calls.append({"stage": f"{label}_entailment", **entail["call_meta"]})
        containment = guardrails.check_asin_containment(
            narrative.get("how_to_choose", ""), allowed_asins, asin,
            foreign_product_names=foreign_product_names,
        )
        if threshold is not None:
            usage = evaluation.compute_usage_rate(
                narrative, experience_snippets, threshold, ruri_url=ruri_url, session=session,
            )
        else:
            usage = {"snippet_rate": None, "article_used": None, "per_snippet": []}
        uniqueness = evaluation.compute_uniqueness(
            narrative, title, tags, corpus_narr_vectors, ruri_url=ruri_url, session=session,
        )
        groups_out[label] = {
            "narrative": narrative,
            "entailment": {"total_unsupported": entail["total_unsupported"], "by_key": entail["by_key"]},
            "asin_containment": containment,
            "usage_rate": usage,
            "uniqueness": uniqueness,
        }

    elapsed = time.time() - t0
    result = {
        "asin": asin,
        "category": category,
        "title": title,
        "elapsed_s": round(elapsed, 1),
        "corpus_size": len(corpus_articles),
        "angle_candidates": angle_out["candidates"],
        "angle_selection": selection,
        "design_memo": memo_out["memo"],
        "critique_flagged": critique_out["flagged"],
        "rewritten_keys": rewrite_out["rewritten_keys"],
        "groups": groups_out,
        "calls": calls,
        "threshold_used": threshold,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{asin}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("done asin=%s elapsed=%.1fs calls=%d", asin, elapsed, len(calls))
    return result


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """群ごとの集計 (p50 max_sim/centroid_sim, 使用率平均, entailment 件数, containment 違反数, コスト)。"""
    summary: dict[str, Any] = {}
    for label in ("A", "B", "C"):
        max_sims, centroid_sims, snippet_rates = [], [], []
        unsupported_total = 0
        containment_violations = 0
        for r in results:
            g = r.get("groups", {}).get(label)
            if not g:
                continue
            uniq = g.get("uniqueness") or {}
            if isinstance(uniq.get("max_sim"), (int, float)):
                max_sims.append(uniq["max_sim"])
            if isinstance(uniq.get("centroid_sim"), (int, float)):
                centroid_sims.append(uniq["centroid_sim"])
            usage = g.get("usage_rate") or {}
            if isinstance(usage.get("snippet_rate"), (int, float)):
                snippet_rates.append(usage["snippet_rate"])
            unsupported_total += (g.get("entailment") or {}).get("total_unsupported", 0)
            if not (g.get("asin_containment") or {}).get("ok", True):
                containment_violations += 1

        summary[label] = {
            "n": sum(1 for r in results if r.get("groups", {}).get(label)),
            "max_sim_p50": _median(max_sims),
            "centroid_sim_p50": _median(centroid_sims),
            "usage_snippet_rate_mean": round(sum(snippet_rates) / len(snippet_rates), 4) if snippet_rates else None,
            "unsupported_statements_total": unsupported_total,
            "asin_containment_violations": containment_violations,
        }

    total_calls = sum(len(r.get("calls", [])) for r in results)
    total_seconds = sum(c.get("total_duration_s", 0) or 0 for r in results for c in r.get("calls", []))
    max_prompt_tokens = max(
        (c.get("estimated_prompt_tokens", 0) or 0 for r in results for c in r.get("calls", [])), default=0,
    )
    summary["cost"] = {
        "asin_count": len(results),
        "total_gemma_calls": total_calls,
        "calls_per_asin_mean": round(total_calls / len(results), 2) if results else None,
        "total_gemma_seconds": round(total_seconds, 1),
        "seconds_per_asin_mean": round(total_seconds / len(results), 1) if results else None,
        "max_estimated_prompt_tokens": max_prompt_tokens,
    }

    def _verdict() -> str:
        a, b, c = summary["A"], summary["B"], summary["C"]
        if a["max_sim_p50"] is None or b["max_sim_p50"] is None:
            return "測れない (max_sim が計算できていない)"
        b_effective = (
            (a["max_sim_p50"] - b["max_sim_p50"]) >= 0.01
            and (b["usage_snippet_rate_mean"] or 0) >= (a["usage_snippet_rate_mean"] or 0)
            and b["unsupported_statements_total"] <= a["unsupported_statements_total"]
        )
        if not b_effective:
            return "B無効 (事前登録した判定基準を満たさない)"
        if c["max_sim_p50"] is None:
            return "B有効 / C測れない"
        c_effective = (
            (b["max_sim_p50"] - c["max_sim_p50"]) >= 0.01
            and (c["usage_snippet_rate_mean"] or 0) >= (b["usage_snippet_rate_mean"] or 0)
            and c["unsupported_statements_total"] <= b["unsupported_statements_total"]
        )
        return "B有効 / C有効" if c_effective else "B有効 / C無効"

    summary["verdict"] = _verdict()
    return summary


def per_asin_light_summary(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PR に載せる用の軽量版 (narrative 本文・call メタは含めない、指標だけ)。"""
    out = []
    for r in results:
        if not r.get("groups"):
            out.append({"asin": r["asin"], "category": r.get("category"), "error": r.get("error")})
            continue
        row: dict[str, Any] = {
            "asin": r["asin"], "category": r["category"], "elapsed_s": r["elapsed_s"],
            "flagged_count": len(r.get("critique_flagged", [])),
            "rewritten_keys": r.get("rewritten_keys", []),
        }
        for label in ("A", "B", "C"):
            g = r["groups"][label]
            row[label] = {
                "max_sim": g["uniqueness"]["max_sim"],
                "centroid_sim": g["uniqueness"]["centroid_sim"],
                "usage_snippet_rate": g["usage_rate"].get("snippet_rate"),
                "unsupported_statements": g["entailment"]["total_unsupported"],
                "asin_containment_ok": g["asin_containment"]["ok"],
            }
        out.append(row)
    return out


def select_sample_asins(results: list[dict[str, Any]], n: int = 2) -> list[dict[str, Any]]:
    """目視確認用のフルサンプルを選ぶ。0件除外後、先頭から均等に n 件取る (再現性のため決め打ち順)。"""
    ok_results = [r for r in results if r.get("groups")]
    if not ok_results:
        return []
    if len(ok_results) <= n:
        return ok_results
    step = len(ok_results) // n
    return [ok_results[i * step] for i in range(n)]


def run(
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    run_dir: pathlib.Path = DEFAULT_RUN_DIR,
    results_out: pathlib.Path = pathlib.Path(DEFAULT_RESULTS_OUT),
    limit: int = 0,
    seed: int = 20260914,
) -> dict[str, Any]:
    selection = select_asins(seed=seed)
    selected = selection["selected"]
    if limit:
        selected = selected[:limit]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    this_run_dir = run_dir / run_id
    session = requests.Session()

    results: list[dict[str, Any]] = []
    cutoff_reason: str | None = None
    broken_count = 0

    for i, asin_info in enumerate(selected, start=1):
        try:
            r = process_asin(
                asin_info, ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx,
                session=session, run_dir=this_run_dir,
            )
        except (GemmaCallError, TruncationError) as e:
            logger.error("asin=%s failed: %s", asin_info["asin"], e)
            r = {"asin": asin_info["asin"], "category": asin_info["category"], "error": str(e), "groups": {}}
        results.append(r)

        if is_broken_narrative(r.get("groups", {}).get("A", {}).get("narrative", {})):
            broken_count += 1
        if broken_count >= BROKEN_ABORT_THRESHOLD and i <= BROKEN_ABORT_WINDOW:
            cutoff_reason = (
                f"group A broken output {broken_count}/{i} ASIN(s) processed "
                f"(threshold {BROKEN_ABORT_THRESHOLD}/{BROKEN_ABORT_WINDOW}) — gemma は書き手に使えない"
            )
            logger.error("cutoff: %s", cutoff_reason)
            break

        if i == TIME_CUTOFF_CHECKPOINT:
            slow = [x for x in results if x.get("elapsed_s", 0) > PER_ASIN_TIME_BUDGET_S]
            if slow:
                cutoff_reason = (
                    f"per-ASIN time budget ({PER_ASIN_TIME_BUDGET_S}s) exceeded within first "
                    f"{TIME_CUTOFF_CHECKPOINT} ASIN(s): {[x['asin'] for x in slow]}"
                )
                logger.error("cutoff: %s", cutoff_reason)
                break

    ok_results = [r for r in results if r.get("groups")]
    summary = summarize(ok_results)
    samples = select_sample_asins(ok_results, n=2)

    # PR に載せるのは集計 + 軽量版 + サンプル2件だけ (各段の生の入出力は run_dir 側、
    # リポジトリ外に残す。#4841 T3 成果物の置き場所の指定)。
    payload = {
        "generated_at": _now_iso(),
        "run_id": run_id,
        "model": model,
        "num_ctx": num_ctx,
        "seed": seed,
        "asin_selection": {
            "candidate_count": selection["candidate_count"],
            "categories_covered": selection["categories_covered"],
            "selected_asins": [s["asin"] for s in selected],
        },
        "asin_count_attempted": len(results),
        "cutoff_reason": cutoff_reason,
        "summary": summary,
        "per_asin_run_dir": str(this_run_dir),
        "per_asin_summary": per_asin_light_summary(results),
        "samples": samples,
    }

    results_out.parent.mkdir(parents=True, exist_ok=True)
    results_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (asin_count=%d, cutoff=%s)", results_out, len(results), cutoff_reason)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    ap.add_argument("--ruri-url", default=DEFAULT_RURI_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    ap.add_argument("--results-out", default=DEFAULT_RESULTS_OUT)
    ap.add_argument("--limit", type=int, default=0, help="ASIN 数の上限 (0=全10件、スモーク用)")
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()

    run(
        ollama_url=args.ollama_url, ruri_url=args.ruri_url, model=args.model, num_ctx=args.num_ctx,
        run_dir=pathlib.Path(args.run_dir), results_out=pathlib.Path(args.results_out),
        limit=args.limit, seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
