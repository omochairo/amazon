"""#4841 V2: 体験談寄りの検索語の試験 — 実行本体。

Usage:
  TAVILY_API_KEY=... python -m scripts.experimental.search_query_trial.run_v2 \\
      --out docs/experience-source-yield/v2_results.json \\
      --run-dir ~/v2_runs/2026-09-16

  # 予算・対象選定だけ確認 (API を叩かない)
  python -m scripts.experimental.search_query_trial.run_v2 --dry-run

本番の data / volume には書かない。experience.json は更新しない (worktree 外の
--run-dir にだけ生の入出力を残す)。**例外は Tavily 消費台帳
(`--base` 配下の `_tavily_usage.json`) だけ**: 本番の予算ガードの根拠を
本レーンの消費ぶんズレさせないため、呼び出し直前に本番と同じ `record_call` で
共有台帳へ書く (owner 修正1)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.fetch_third_party_sources import month_usage
from scripts.mine_experience import make_session, resolve_product_identity

from scripts.experimental.search_query_trial import (
    asin_selection,
    budget,
    evaluation,
    gather,
    mining,
    query_groups,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("search_query_trial.run_v2")

DEFAULT_PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_OUT = pathlib.Path("docs/experience-source-yield/v2_results.json")
SOURCE_TYPE = "blog"  # gather_third_party が third_party_sources.json 由来に付ける値と揃える


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair_result_path(run_dir: pathlib.Path, asin: str, group: str) -> pathlib.Path:
    return run_dir / f"{asin}_{group}.json"


def run_one_group(
    group: str, asin: str, api_key: str | None, *,
    base: pathlib.Path, session: requests.Session,
    ollama_url: str, model: str, num_ctx: int,
    run_dir: pathlib.Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """1 ASIN・1群分を実行し、(stats, raw_record) を返す。"""
    title, product_name, brand = resolve_product_identity(asin, base)

    if group == "Q0":
        urls = gather.q0_urls(asin, base)
        query_record: dict[str, Any] = {"group": "Q0", "query": None, "new_queries": 0}
    else:
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY 未設定 — Q1/Q2 は新規 query が必要")
        search = query_groups.search_for_group(group, asin, api_key, base=base)
        urls = [s["url"] for s in search["sources"]]
        query_record = {
            "group": group, "query": search["query"],
            "include_domains": search.get("include_domains"),
            "new_queries": 1,
        }

    candidates, fetch_log = gather.fetch_bodies(urls, source_type=SOURCE_TYPE, session=session)
    url_texts = {row["source_url"]: row["text"] for row in candidates}

    snippets: list[dict] = []
    extraction_meta: list[dict] = []
    for c in candidates:
        s, meta = mining.extract_snippets_checked(
            c, product_name, brand, ollama_url=ollama_url, model=model,
            num_ctx=num_ctx, session=session,
        )
        snippets += s
        extraction_meta.append({"source_url": c["source_url"], **meta})

    stats = evaluation.compute_group_stats(
        asin=asin, group=group, tried_urls=urls, fetch_log=fetch_log,
        snippets=snippets, product_name=product_name, brand=brand,
        url_texts=url_texts, extraction_meta=extraction_meta,
    )

    raw_record = {
        "asin": asin, "title": title, "product_name": product_name, "brand": brand,
        "query": query_record, "fetch_log": fetch_log, "snippets": snippets,
        "extraction_meta": extraction_meta,
    }
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = _pair_result_path(run_dir, asin, group)
        payload = {"status": "ok", "stats": stats, **raw_record}
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return stats, raw_record


def run(
    asins: list[str], *,
    base: pathlib.Path = DEFAULT_PER_ASIN_DIR,
    api_key: str | None,
    ollama_url: str,
    model: str,
    num_ctx: int,
    run_dir: pathlib.Path | None,
    sleeper=time.sleep,
    resume: bool = False,
) -> dict[str, Any]:
    """owner 修正2: ASIN×群の単位で例外を捕まえ、その組を失敗として記録して続行する
    (Tavily の 429/5xx がそのまま run 全体を落とさないように)。`--run-dir` に前回の
    結果があれば (resume=True のとき) Tavily を叩き直さない。"""
    session, dead_hosts = make_session(ollama_url)
    per_asin_group_stats: dict[str, dict[str, dict[str, Any]]] = {}
    query_log: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    usage_before = month_usage(base)

    for asin in asins:
        per_asin_group_stats[asin] = {}
        for group in query_groups.GROUPS:
            result_path = _pair_result_path(run_dir, asin, group) if run_dir is not None else None

            if resume and result_path is not None and result_path.exists():
                cached = json.loads(result_path.read_text(encoding="utf-8"))
                if cached.get("status") == "error":
                    failures.append({"asin": asin, "group": group, "error": cached.get("error")})
                    logger.info("%s %s: resume — 前回失敗のためスキップ (%s)", asin, group, cached.get("error"))
                    continue
                per_asin_group_stats[asin][group] = cached["stats"]
                query_log.append(cached.get("query", {}))
                logger.info("%s %s: resume — 既存結果を再利用 (Tavilyは叩かない)", asin, group)
                continue

            try:
                stats, raw_record = run_one_group(
                    group, asin, api_key, base=base, session=session,
                    ollama_url=ollama_url, model=model, num_ctx=num_ctx, run_dir=run_dir,
                )
            except Exception as e:
                logger.error("%s %s: 失敗 — この組を諦めて続行 (%s)", asin, group, e)
                failures.append({"asin": asin, "group": group, "error": str(e)})
                if run_dir is not None:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    result_path.write_text(json.dumps(
                        {"status": "error", "error": str(e)}, ensure_ascii=False, indent=2,
                    ), encoding="utf-8")
                continue

            per_asin_group_stats[asin][group] = stats
            query_log.append(raw_record["query"])
            logger.info(
                "%s %s: tried=%d success=%d snippets=%d",
                asin, group, stats["urls_tried"], stats["urls_fetch_success"],
                stats["snippet_count"],
            )
        sleeper(1)  # ASIN 間の礼儀 (Tavily への負荷を均す)

    report = evaluation.build_report(per_asin_group_stats)
    new_query_count = sum(1 for q in query_log if q.get("new_queries"))
    report["new_query_count"] = new_query_count
    report["dead_hosts"] = dead_hosts.summary()
    report["asins"] = asins
    report["generated_at"] = _now_iso()
    report["failures"] = failures
    # owner 修正1: 今回の消費を報告に出す。台帳 (base/_tavily_usage.json) は
    # query_groups.tavily_search が呼び出し直前に直接更新しているので、ここでは
    # 差分を読み直して報告するだけ (owner がdata PRへ反映する際の根拠になる)。
    usage_after = month_usage(base)
    report["tavily_calls_consumed"] = usage_after - usage_before
    report["tavily_usage_before"] = usage_before
    report["tavily_usage_after"] = usage_after
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=str(DEFAULT_PER_ASIN_DIR))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--run-dir", default=str(pathlib.Path.home() / "v2_runs" /
                                              datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    ap.add_argument("--limit", type=int, default=asin_selection.DEFAULT_TARGET_COUNT)
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--model", default=os.environ.get("EXPERIENCE_MODEL", "gemma4:26b-a4b-it-qat"))
    ap.add_argument("--num-ctx", type=int, default=int(os.environ.get("OLLAMA_NUM_CTX", 8192)))
    ap.add_argument("--monthly-budget", type=int, default=budget.DEFAULT_MONTHLY_BUDGET)
    ap.add_argument("--daily-pace", type=float, default=budget.DEFAULT_DAILY_PACE)
    ap.add_argument("--dry-run", action="store_true",
                     help="予算チェックと対象選定のみ表示し、API/gemma は叩かない")
    ap.add_argument("--resume", action="store_true",
                     help="--run-dir に前回の結果がある ASIN×群は Tavily を叩き直さない")
    args = ap.parse_args()

    base = pathlib.Path(args.base)

    # owner 修正6: 台帳は最大1日ぶん古くなりうる (前回 main 反映以降の既存レーン消費が
    # 乗らない)。着手直前に origin/main を fetch して読み直す。fetch できない
    # サンドボックス環境ではローカルファイルにフォールックする。
    usage_data = budget.refresh_ledger_from_origin_main()
    ledger_source = "origin/main (git fetch 済み)" if usage_data is not None else \
        "local file (git fetch 失敗 — フォールバック)"
    logger.info("Tavily 台帳の参照元: %s", ledger_source)

    budget_report = budget.check_tavily_budget(
        monthly_budget=args.monthly_budget, daily_pace=args.daily_pace, usage_data=usage_data,
    )
    budget_report["ledger_source"] = ledger_source
    logger.info("Tavily budget: %s", json.dumps(budget_report, ensure_ascii=False))
    if not budget_report["feasible"]:
        logger.error("Tavily 月次予算を超える見込み — 止まって報告する (前提①)")
        print(json.dumps({"status": "aborted_budget", "budget": budget_report},
                          ensure_ascii=False, indent=2))
        return 1

    selection = asin_selection.select_v2_asins(target_count=args.limit)
    logger.info(
        "対象 ASIN: %d 件 (候補 %d 件, カテゴリ %s)",
        len(selection["selected"]), selection["candidate_count"], selection["categories_covered"],
    )
    if not selection["meets_min_categories"]:
        logger.warning("カテゴリ多様性 (>=3) を満たしていない — 報告して判断を仰ぐこと")

    asins = [c["asin"] for c in selection["selected"]]

    if args.dry_run:
        print(json.dumps({
            "status": "dry_run", "budget": budget_report, "selection": selection,
        }, ensure_ascii=False, indent=2))
        return 0

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        logger.error("TAVILY_API_KEY 未設定 — 中断 (キーの値はログに出さない)")
        return 1

    run_dir = pathlib.Path(args.run_dir)
    report = run(
        asins, base=base, api_key=api_key, ollama_url=args.ollama_url,
        model=args.model, num_ctx=args.num_ctx, run_dir=run_dir, resume=args.resume,
    )
    report["selection"] = selection
    report["budget"] = budget_report

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("書き出し: %s (生データは %s)", out_path, run_dir)
    logger.info("決定: %s", report["decision"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
