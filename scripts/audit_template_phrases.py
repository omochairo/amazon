"""audit_template_phrases.py

amazon-navi-brain#39 Step 1 の "例文複写の禁止" に着手する前に測る観測専用ベースライン。

なぜ quality_gate.py の check として実装しないか:
  quality_gate.CheckResult.score は ArticleReport.total_score (全 check の平均)
  に直接効く。レビュー指示は「score/passed に影響させない」観測専用チェックを
  求めているが、既存の quality_census (#4828) の「減点のみ」集計
  (`deducted_checks`) は ``passed and score < 1.0`` の check しか拾わない。
  つまり quality_gate に足すなら「score を 1.0 のまま報告に乗せる」か
  「score を落として真に観測専用ではなくす」かの二択になり、どちらも指示の
  意図（真に中立・かつ可視化される）を素直に満たせない。narrative の型・語彙を
  スキャンする作業は記事単体の合否判定 (PR 時) とも性質が違う (コーパス横断の
  週次観測)ので、audit_uniqueness.py / audit_query_entailment.py と同じ
  「独立した監査スクリプト」を新設する方が筋が良い。

測る対象 (PROMPT_TEMPLATE.md §1.B / §5.A の例文からの逐語コピー):
  amazon-navi-brain#39 コメントで実測された定型句 (pre_v7 → post_v7 で出現率が
  下がっていない/上がっている句を含む、2026-09-11 実測でリスト確定)。
  §5.A の例文 (「どんな知育効果があるのか」「気になる方も多い」等) と
  §1.B の hook パターン例文 (「本記事の結論は」「頭一つ抜けています」等) の
  逐語コピーを検出する。

cohort: scripts.audit_uniqueness.cohort3_for_entry を再利用し、pre_v7 /
post_v7_new / post_v7_rewrite の3分割で集計する (#39 Step 0 の成果物を再利用)。
K8/embedding 不要 (単純な部分文字列一致) — GitHub Actions (ubuntu-latest) だけで
完結する。

出力: data/analytics/template_phrase_audit.json に
  {generated_at, source_week, corpus_size, cohort_sizes, phrases: [{id, text,
   origin, hits: {cohort: count}, rate: {cohort: 0-1 float}}]}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

from scripts.audit_uniqueness import (
    DEFAULT_REWRITE_LEDGER,
    cohort3_for_entry,
    load_rewrite_ledger,
)
from scripts.compute_semantic_related import DEFAULT_ARTICLES_DIR, discover_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_template_phrases")

DEFAULT_OUT = "data/analytics/template_phrase_audit.json"

COHORTS: tuple[str, ...] = ("pre_v7", "post_v7_new", "post_v7_rewrite")

_NARRATIVE_KEYS = (
    "lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose",
)

# amazon-navi-brain#39 (2026-09-11) 実測ベースの候補句。§1.B (hook 3パターン) /
# §5.A (4-step 例文) からの逐語引用。id は quality_census 等での安定キー。
KNOWN_PHRASES: tuple[dict[str, str], ...] = (
    {"id": "hook_a_conclusion", "text": "本記事の結論は", "origin": "§1.B hookパターンA例文"},
    {"id": "hook_c_ahead", "text": "頭一つ抜けています", "origin": "§1.B hookパターンC例文"},
    {"id": "why_edu_question", "text": "どんな知育効果があるのか", "origin": "§5.A why_this_product 問い例文"},
    {"id": "generic_concern_1", "text": "気になる方も多い", "origin": "§5.A 問いブロック常套句"},
    {"id": "generic_concern_2", "text": "気になる方が多いと思います", "origin": "§5.A 問いブロック常套句"},
    {"id": "gift_appeal_summary", "text": "外しにくい", "origin": "§5.A gift_appeal 例文 (「外しにくさ」を含む)"},
    {"id": "daily_use_not_bored", "text": "すぐに飽きてしまわない", "origin": "§5.A daily_use 常套句"},
    {"id": "closing_generic", "text": "結局のところ、この商品", "origin": "§5.A closing 常套句"},
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_week_label(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def build_phrase_text(article: dict[str, Any]) -> str:
    """narrative 全セクションを連結する (title/tags は語彙が固定的すぎるため対象外)。"""
    if not isinstance(article, dict):
        return ""
    narrative = article.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    parts: list[str] = []
    for key in _NARRATIVE_KEYS:
        v = narrative.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.append(" ".join(str(x) for x in v if isinstance(x, str)))
    return "\n".join(parts)


def count_hits(text: str, phrases: tuple[dict[str, str], ...] = KNOWN_PHRASES) -> dict[str, bool]:
    return {p["id"]: p["text"] in text for p in phrases}


def run(
    articles_dir: pathlib.Path,
    out_path: pathlib.Path,
    *,
    rewrite_ledger_path: str | os.PathLike[str] = DEFAULT_REWRITE_LEDGER,
    limit: int = 0,
) -> dict[str, Any]:
    """全記事を走査し、句ごと・cohort ごとの出現件数を集計して書き出す。"""
    paths = discover_articles(articles_dir)
    asins = sorted(paths.keys())
    if limit and limit > 0:
        asins = asins[:limit]

    ledger = load_rewrite_ledger(rewrite_ledger_path)
    cohort_sizes: dict[str, int] = {c: 0 for c in COHORTS}
    phrase_hits: dict[str, dict[str, int]] = {p["id"]: {c: 0 for c in COHORTS} for p in KNOWN_PHRASES}

    processed = 0
    for asin in asins:
        path = paths[asin]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse %s: %s", asin, path, e)
            continue
        if not isinstance(data, dict):
            continue
        slug = data.get("slug")
        slug = slug if isinstance(slug, str) and slug else path.stem
        cohort = cohort3_for_entry(asin, slug, ledger)
        cohort_sizes[cohort] += 1
        processed += 1

        text = build_phrase_text(data)
        hits = count_hits(text)
        for phrase_id, hit in hits.items():
            if hit:
                phrase_hits[phrase_id][cohort] += 1

    phrases_out = []
    for p in KNOWN_PHRASES:
        hits = phrase_hits[p["id"]]
        rate = {
            c: (round(hits[c] / cohort_sizes[c], 4) if cohort_sizes[c] else None)
            for c in COHORTS
        }
        phrases_out.append({**p, "hits": hits, "rate": rate})

    payload: dict[str, Any] = {
        "generated_at": _now_iso(),
        "source_week": _iso_week_label(datetime.now(timezone.utc)),
        "corpus_size": processed,
        "cohort_sizes": cohort_sizes,
        "phrases": phrases_out,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s: corpus=%d cohort_sizes=%s", out_path, processed, cohort_sizes)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--rewrite-ledger", default=DEFAULT_REWRITE_LEDGER)
    ap.add_argument("--limit", type=int, default=0, help="処理する記事数の上限 (0=全件、スモークテスト用)")
    args = ap.parse_args()

    run(
        pathlib.Path(args.articles_dir),
        pathlib.Path(args.out),
        rewrite_ledger_path=args.rewrite_ledger,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
