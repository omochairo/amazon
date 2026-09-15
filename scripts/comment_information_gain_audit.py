"""comment_information_gain_audit.py

#4841 S3「物差し (M1) を本番の週次監査に載せる」のサーフェシングスクリプト。

scripts/audit_information_gain.py の出力 (``data/analytics/information_gain_audit.json``)
を、既存の tracker issue #3300 (凡庸度の週次監査 tracker) へ週次ロールアップコメント
として追記する。**新しい tracker issue は作らない** (#4841 実装依頼 S3 の明示指示)。

凡庸度の週次コメント (scripts/comment_uniqueness_audit.py) とは別のマーカー
(``<!-- information-gain-audit:<source_week> -->``) を使うことで、同じ issue に
両方のロールアップが共存できるようにする。

このレーンの位置づけ: **観測専用。自動リライト等には未連動。**

実行環境:
  - 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon)
  - gh CLI が PATH にあること
  - 副作用は Issue コメント追記のみ。新規 Issue 起票はしない

呼び出し元: omochairo/amazon-home-ops の週次 workflow
Issue: https://github.com/omochairo/amazon/issues/4841 (S3) / #3300 (tracker)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from typing import Any

from scripts import gh_rest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("comment_information_gain_audit")

DEFAULT_IN = "data/analytics/information_gain_audit.json"
WEEK_MARKER_PREFIX = "information-gain-audit:"
# #3300 は元々「凡庸度の週次監査 tracker」だが、#4841 実装依頼 S3 により
# 新規 issue を起票せずここに相乗りする (別マーカーで共存)。
DEFAULT_TRACKER_ISSUE = 3300


def has_existing_week_comment(repo: str, issue_number: int, source_week: str) -> bool:
    """指定 Issue の既存コメントに、この週用のマーカーが既にあるか。"""
    marker = f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->"
    res = gh_rest.run_gh(
        ["api", "-X", "GET", f"repos/{repo}/issues/{issue_number}/comments", "-f", "per_page=100"],
    )
    comments = json.loads(res.stdout)
    if not isinstance(comments, list):
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def _fmt_num(v: Any) -> str:
    if not isinstance(v, (int, float)):
        return "n/a"
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _fmt_pct(v: Any) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "n/a"


def render_material_row(label: str, group: dict[str, Any] | None) -> str:
    group = group or {}
    return (
        f"| {label} | {group.get('count', 0)} | "
        f"{_fmt_num(group.get('median_unique_and_supported_count'))} | "
        f"{_fmt_pct(group.get('median_unique_and_supported_rate'))} |"
    )


def render_comment_body(payload: dict[str, Any]) -> str:
    source_week = payload.get("source_week", "")
    model = payload.get("model", "")
    summary = payload.get("summary") or {}
    by_material = summary.get("by_experience_material") or {}
    classification = summary.get("unsupported_classification_totals") or {}
    failed = payload.get("failed") or []

    parts = [
        f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->",
        "## 🧮 情報利得の週次監査 (「固有かつ裏付けありの文」)",
        "",
        "**観測専用 — この監査は自動リライトには未連動です。** #4841 M1 で採用した物差し"
        "(narrative を文単位に分割し、ASIN の素材から導けるか(裏付け)と既存コーパスに無いか"
        "(固有性)を測る) を週次で計測し、素材供給 (体験談レーン等) の変化が記事の情報利得に"
        "効いているかを定点観測します。",
        "",
        f"モデル: `{model}` / 週: `{source_week}` / "
        f"対象 {summary.get('target_count', 0)} 件・処理 {summary.get('processed_count', 0)} 件・"
        f"失敗 {summary.get('failed_count', 0)} 件 ({_fmt_pct(summary.get('failure_ratio'))})",
        "",
        f"### 固有かつ裏付けありの文 (全体、中央値)",
        "",
        f"数: {_fmt_num(summary.get('median_unique_and_supported_count'))} / "
        f"率: {_fmt_pct(summary.get('median_unique_and_supported_rate'))}",
        "",
        "### experience.json の有無別 (群比較)",
        "",
        "| 群 | 件数 | 数の中央値 | 率の中央値 |",
        "|---|---:|---:|---:|",
        render_material_row("あり (生成時点で使用可能)", by_material.get("with_material")),
        render_material_row("なし", by_material.get("without_material")),
        render_material_row("判定不能", by_material.get("unknown")),
        "",
        "### 裏付けの無い文の分類",
        "",
        f"修辞・時点依存: {classification.get('rhetorical_or_time_dependent', 0)} 件 / "
        f"事実の主張: {classification.get('factual_claim', 0)} 件 / "
        f"未分類: {classification.get('unresolved', 0)} 件",
        "",
    ]

    if failed:
        parts += [
            f"### 失敗した記事 ({len(failed)} 件)",
            "",
            "| ASIN | 理由 |",
            "|---|---|",
        ]
        for f in failed[:20]:
            parts.append(f"| `{f.get('asin', '')}` | {f.get('reason', '')} |")
        if len(failed) > 20:
            parts.append(f"| ... | 他 {len(failed) - 20} 件 |")
        parts.append("")

    parts += [
        "> ⚠️ 判定はローカル LLM (K8/gemma・Ruri) による参考値です。#4841 S3。",
        "",
        f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->",
    ]
    return "\n".join(parts)


def post_comment(repo: str, issue_number: int, body: str) -> None:
    gh_rest.post_issue_comment(repo, issue_number, body)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--issue-number", type=int,
                    default=int(os.environ.get("TRACKER_ISSUE", DEFAULT_TRACKER_ISSUE)))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2

    payload = json.loads(in_path.read_text(encoding="utf-8"))
    source_week = payload.get("source_week", "")
    if not source_week:
        logger.error("input payload missing source_week — refusing to post ambiguous comment")
        return 2

    if has_existing_week_comment(args.repo, args.issue_number, source_week):
        logger.info("skip (already commented for week %s): issue #%d", source_week, args.issue_number)
        return 0

    body = render_comment_body(payload)
    if args.dry_run:
        print(f"--- would comment on issue #{args.issue_number} (week {source_week}) ---")
        print(body)
        return 0

    post_comment(args.repo, args.issue_number, body)
    logger.info("commented on issue #%d: week %s", args.issue_number, source_week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
