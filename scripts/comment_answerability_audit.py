"""comment_answerability_audit.py

Issue #2995 消費側②「GSC 回答性 entailment 監査」のサーフェシングスクリプト。

scripts/audit_query_entailment.py の出力 (``data/analytics/answerability_audit.json``)
を読み、各ページに対応する既存の低 CTR Issue (scripts/open_low_ctr_issues.py が
marker `a1-low-ctr:<URL>` 付きで起票したもの) を検索し、gemma judge の回答性診断を
**コメントとして 1 回だけ**追記する。

このフェーズの位置づけ:
  #2928/#2929 のようなサンプル数の少ない検出に対して自動リライト要否を判断するのは
  時期尚早なため、新規 Issue は作らず既存 Issue へのコメント追記に留める
  (診断コメントには「検証フェーズ / 自動リライトには未連動」と明記する)。
  owner が gemma の判定妥当性を目視評価し、妥当と確認できたら次フェーズで
  Jules リライトパイプラインへ missing_aspects を連携する。

重複コメント防止:
  各コメント本文に `<!-- answerability-audit:<URL> -->` マーカーを埋め込み、
  投稿前に対象 Issue の既存コメントを検索して同マーカーがあれば skip する。

実行環境:
  - 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon)
  - gh CLI が PATH にあること
  - 副作用は Issue コメント追記のみ。新規 Issue 起票はしない
    (CLAUDE.md の GitHub API バースト禁止規律に沿う)

呼び出し元: omochairo/amazon-home-ops .github/workflows/22-answerability-audit.yml
Issue: https://github.com/omochairo/amazon/issues/2995 (消費側②) / #2928 / #2929
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("comment_answerability_audit")

DEFAULT_IN = "data/analytics/answerability_audit.json"
LOW_CTR_MARKER_PREFIX = "a1-low-ctr:"
AUDIT_MARKER_PREFIX = "answerability-audit:"
LOW_CTR_SEARCH_LABELS = ("quality", "analytics")


def find_low_ctr_issue_numbers(repo: str) -> dict[str, int]:
    """label=quality,analytics の open Issue から {URL: issue番号} を作る。

    scripts.open_low_ctr_issues.find_existing_taken_urls と同じ検索クエリ
    (marker `a1-low-ctr:<URL>` を本文に含む open Issue) を使うが、URL の集合では
    なく issue 番号までマップする点が異なる (コメント投稿先を特定するため)。
    """
    labels = " ".join(f"label:{l}" for l in LOW_CTR_SEARCH_LABELS)
    query = f'repo:{repo} is:issue is:open {labels} in:body "{LOW_CTR_MARKER_PREFIX}"'
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=100"],
        check=True, capture_output=True, text=True,
    )
    items = json.loads(res.stdout).get("items", [])
    mapping: dict[str, int] = {}
    for it in items:
        number = it.get("number")
        body = it.get("body") or ""
        idx = body.find(LOW_CTR_MARKER_PREFIX)
        while idx >= 0:
            tail = body[idx + len(LOW_CTR_MARKER_PREFIX):]
            url = tail.split("-->", 1)[0].strip()
            if url and isinstance(number, int):
                mapping[url] = number
            idx = body.find(LOW_CTR_MARKER_PREFIX, idx + 1)
    return mapping


def has_existing_audit_comment(repo: str, issue_number: int, url: str) -> bool:
    """指定 Issue の既存コメントに、この URL 用の audit marker が既にあるか。"""
    marker = f"<!-- {AUDIT_MARKER_PREFIX}{url} -->"
    res = subprocess.run(
        ["gh", "api", "-X", "GET", f"repos/{repo}/issues/{issue_number}/comments",
         "-f", "per_page=100"],
        check=True, capture_output=True, text=True,
    )
    comments = json.loads(res.stdout)
    if not isinstance(comments, list):
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def _fmt_answered(v: bool | None) -> str:
    if v is True:
        return "✅ 回答あり"
    if v is False:
        return "❌ 回答不足"
    return "⚠️ 判定失敗"


def _fmt_coverage(v: float | None) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "n/a"


def render_query_rows(queries: list[dict]) -> str:
    if not queries:
        return "| (no query data) | - | - | - | - | - |"
    rows = []
    for q in queries:
        missing = q.get("missing_aspects") or []
        missing_s = "、".join(str(m) for m in missing) if missing else "-"
        rows.append(
            f"| `{q.get('query', '')}` | {q.get('impressions', 0)} | {q.get('position', 0.0):.1f} | "
            f"{_fmt_answered(q.get('answered'))} | {_fmt_coverage(q.get('coverage'))} | {missing_s} |"
        )
    return "\n".join(rows)


def render_comment_body(page_entry: dict[str, Any], *, model: str, source_week: str) -> str:
    url = page_entry["page"]
    coverage_avg = _fmt_coverage(page_entry.get("page_coverage_avg"))
    queries = page_entry.get("queries") or []

    parts = [
        f"<!-- {AUDIT_MARKER_PREFIX}{url} -->",
        "## ⚙️ ローカルAI (gemma4) による回答性の自動診断",
        "",
        "**検証フェーズ — この診断は自動リライトには未連動です。** "
        "サンプル数がまだ少ないため、リライト要否の自動判断ではなく、"
        "ローカル LLM (K8 LLM ワーカー) による entailment 判定の妥当性検証を目的としています。",
        "",
        f"モデル: `{model}` / GSC データ週: `{source_week}` / ページ平均カバレッジ: {coverage_avg}",
        "",
        "| クエリ | impressions | position | 回答性 | カバレッジ | 不足している観点 |",
        "|---|---:|---:|---|---:|---|",
        render_query_rows(queries),
        "",
        "> ⚠️ 判定はローカル LLM (gemma4:26b-a4b-it-qat) による参考値です。"
        "リライトの要否は owner の判断に委ねます。",
        "",
        f"<!-- {AUDIT_MARKER_PREFIX}{url} -->",
    ]
    return "\n".join(parts)


def post_comment(repo: str, issue_number: int, body: str) -> None:
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "-R", repo, "--body", body],
        check=True, capture_output=True, text=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--dry-run", action="store_true",
                   help="投稿は行わず、コメント本文を stdout に出すのみ")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2

    payload = json.loads(in_path.read_text(encoding="utf-8"))
    pages = payload.get("pages") or []
    if not pages:
        logger.info("no audited pages — exiting")
        return 0

    model = payload.get("model", "")
    source_week = payload.get("source_week", "")

    issue_map = find_low_ctr_issue_numbers(args.repo)
    logger.info("existing low-CTR open Issues with marker: %d", len(issue_map))

    commented = 0
    for page_entry in pages:
        url = page_entry.get("page")
        if not url:
            continue
        issue_number = issue_map.get(url)
        if issue_number is None:
            logger.info("skip (no matching open low-CTR Issue): %s", url)
            continue
        if has_existing_audit_comment(args.repo, issue_number, url):
            logger.info("skip (already commented): %s (issue #%d)", url, issue_number)
            continue

        body = render_comment_body(page_entry, model=model, source_week=source_week)
        if args.dry_run:
            print(f"--- would comment on issue #{issue_number} ({url}) ---")
            print(body)
            print()
            continue
        post_comment(args.repo, issue_number, body)
        logger.info("commented on issue #%d: %s", issue_number, url)
        commented += 1

    logger.info("posted %d comment(s)", commented)
    return 0


if __name__ == "__main__":
    sys.exit(main())
