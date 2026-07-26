"""comment_uniqueness_audit.py

Issue #3203 Phase 3「凡庸度の定量監査」のサーフェシングスクリプト。

scripts/audit_uniqueness.py の出力 (``data/analytics/uniqueness_audit.json``) を、
専用の観察 issue (tracker issue) 1 本に**週次ロールアップコメント**として追記する。

なぜ専用 issue 1 本か (owner 判断・2026-07-16):
  answerability 監査 (#2995 消費側②) は「ページ単位」の診断のためページ毎に対応する
  既存 low-CTR Issue へコメントする設計だが、uniqueness 監査は「コーパス全体の分布」を
  見るものであり対応する記事単位 Issue が無い。新規 Issue を毎週起票すると GitHub API
  バースト禁止規律 (CLAUDE.md 2026-07-08 制定) に反するため、tracker issue を 1 本だけ
  事前に手動起票し (本文に marker `uniqueness-audit-tracker` を含める)、以後は同じ
  Issue に週次でコメントを追記する。

重複コメント防止:
  各コメント本文に `<!-- uniqueness-audit:<source_week> -->` マーカーを埋め込み、
  投稿前に対象 Issue の既存コメントを検索して同じ週のマーカーがあれば skip する
  (answerability 監査の URL 単位マーカーと同型・週単位に変えたもの)。

このフェーズの位置づけ:
  「検証フェーズ — 自動リライトには未連動」(answerability 監査と同じ規律)。
  cohort_stats (pre_v7/post_v7 の分布比較) で v7 施行の効果を、flagged (閾値超え
  記事の一覧) で将来のリライト候補選定の新シグナルを提供するのみ。

実行環境:
  - 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon)
  - gh CLI が PATH にあること
  - 副作用は Issue コメント追記のみ。新規 Issue 起票はしない
    (tracker issue が見つからない場合は warning + skip、スクリプトからの起票はしない)

呼び出し元: omochairo/amazon-home-ops .github/workflows/24-uniqueness-audit.yml
Issue: https://github.com/omochairo/amazon/issues/3203 (Phase 3)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("comment_uniqueness_audit")

DEFAULT_IN = "data/analytics/uniqueness_audit.json"
TRACKER_MARKER = "uniqueness-audit-tracker"
WEEK_MARKER_PREFIX = "uniqueness-audit:"
TRACKER_SEARCH_LABELS = ("observation", "quality")

# owner が手動起票した恒久 tracker issue (#3300)。GitHub App installation token 経由の
# Search API は「新規作成されたばかりの issue」を数日単位で 0 件のまま返し続けることがあり
# (feedback-omochairo-gh-search-issues-index-lag)、search/issues で毎回探す設計だと
# 自動ロールアップが恒久的に skip され続けてしまう。tracker は番号が固定なので、Search では
# なく番号直指定を既定にする (コメント取得/投稿の REST API は App token でも正常に効く)。
DEFAULT_TRACKER_ISSUE = 3300

_SEARCH_MAX_ATTEMPTS = 3
_SEARCH_RETRY_SLEEP_SECONDS = 3.0


def _search_issues(query: str, sleeper=time.sleep) -> list[dict]:
    """`gh api search/issues` を実行し items を返す。

    GitHub Search API のインデックス反映ラグ対策
    (scripts.comment_answerability_audit._search_issues と同型)。直前の書き込み
    (この workflow 自身が作る data PR 等) 直後は既存 issue が一時的にヒットしない
    ことがあるため、total_count=0 の場合のみ短い間隔で再試行する (真に0件のケースは
    再試行しても変わらずそのまま受け入れる)。
    """
    items: list[dict] = []
    for attempt in range(1, _SEARCH_MAX_ATTEMPTS + 1):
        res = subprocess.run(
            ["gh", "api", "-X", "GET", "search/issues",
             "-f", f"q={query}", "-f", "per_page=100"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        items = json.loads(res.stdout).get("items", [])
        if items or attempt == _SEARCH_MAX_ATTEMPTS:
            return items
        logger.info("search/issues returned 0 items (attempt %d/%d); retrying "
                    "in case of search index lag", attempt, _SEARCH_MAX_ATTEMPTS)
        sleeper(_SEARCH_RETRY_SLEEP_SECONDS)
    return items


def find_tracker_issue_number(repo: str, sleeper=time.sleep) -> int | None:
    """本文に marker `uniqueness-audit-tracker` を含む open Issue を 1 本探す。

    複数該当した場合は issue 番号が最小のもの (= 最初に起票されたもの) を採る。
    見つからなければ None (呼び出し元は新規起票せず warning + skip する)。
    """
    labels = " ".join(f"label:{l}" for l in TRACKER_SEARCH_LABELS)
    query = f'repo:{repo} is:issue is:open {labels} in:body "{TRACKER_MARKER}"'
    items = _search_issues(query, sleeper=sleeper)
    numbers = [it.get("number") for it in items if isinstance(it.get("number"), int)]
    return min(numbers) if numbers else None


def resolve_tracker_issue_number(
    repo: str, issue_number: int | None, sleeper=time.sleep
) -> int | None:
    """使用する tracker issue 番号を決める。

    正の ``issue_number`` が与えられていれば Search を介さずそれを直接使う (既定運用)。
    ``issue_number`` が None/0 以下のときのみ、従来の marker Search
    (:func:`find_tracker_issue_number`) にフォールバックする — issue 番号が将来
    変わった場合の逃げ道として残す。
    """
    if issue_number and issue_number > 0:
        return issue_number
    return find_tracker_issue_number(repo, sleeper=sleeper)


def has_existing_week_comment(repo: str, issue_number: int, source_week: str) -> bool:
    """指定 Issue の既存コメントに、この週用のマーカーが既にあるか。"""
    marker = f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->"
    res = subprocess.run(
        ["gh", "api", "-X", "GET", f"repos/{repo}/issues/{issue_number}/comments",
         "-f", "per_page=100"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    comments = json.loads(res.stdout)
    if not isinstance(comments, list):
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "n/a"


def _fmt_num(v) -> str:
    """百分位の表示用。95.0 → "95"、95.5 → "95.5"。"""
    if not isinstance(v, (int, float)):
        return "?"
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def render_cohort_rows(cohort_stats: dict[str, Any]) -> str:
    rows = []
    for cohort, label in (
        ("pre_v7", "pre_v7 (施行前)"),
        ("post_v7", "post_v7 (施行後)"),
        ("all", "**all (コーパス全体)**"),
    ):
        s = cohort_stats.get(cohort)
        if s is None:
            # pre_v7 / post_v7 は欠けていても 0 行として出す (既存の契約)。
            # all は旧スキーマの JSON には存在しないので、その場合だけ行を省く。
            if cohort == "all":
                continue
            s = {}
        rows.append(
            f"| {label} | {s.get('count', 0)} | {_fmt_pct(s.get('max_sim_p50'))} | "
            f"{_fmt_pct(s.get('max_sim_p90'))} | {_fmt_pct(s.get('centroid_sim_p50'))} | "
            f"{_fmt_pct(s.get('centroid_sim_p90'))} |"
        )
    return "\n".join(rows)


def render_threshold_line(payload: dict[str, Any]) -> str:
    """しきい値の説明行。percentile モードでは実効値と絶対基準の超過件数を併記する。"""
    thresholds = payload.get("thresholds") or {}
    mode = thresholds.get("mode", "absolute")
    corpus_size = payload.get("corpus_size", 0) or 0
    flagged_total = payload.get("flagged_total")

    eff = (
        f"flagged 実効閾値: max_sim>{thresholds.get('max_sim')}, "
        f"centroid_sim>{thresholds.get('centroid_sim')}"
    )
    if mode == "percentile":
        eff += (
            f" (分布ベース: それぞれコーパスの p{_fmt_num(thresholds.get('max_sim_percentile'))} / "
            f"p{_fmt_num(thresholds.get('centroid_sim_percentile'))})"
        )
    if flagged_total is not None:
        eff += f" — 該当 {flagged_total} 件"

    ref = thresholds.get("absolute_reference") or {}
    if ref:
        def _pct_of(n) -> str:
            if not isinstance(n, int) or not corpus_size:
                return ""
            return f" ({n / corpus_size * 100:.1f}%)"

        eff += (
            f"\n\n絶対基準での超過件数 (進捗追跡用・閾値固定): "
            f"max_sim>{ref.get('max_sim')} → {ref.get('max_sim_exceeded')} 件"
            f"{_pct_of(ref.get('max_sim_exceeded'))}, "
            f"centroid_sim>{ref.get('centroid_sim')} → {ref.get('centroid_sim_exceeded')} 件"
            f"{_pct_of(ref.get('centroid_sim_exceeded'))}"
        )
    return eff


def render_flagged_rows(flagged: list[dict[str, Any]]) -> str:
    if not flagged:
        return "| (閾値超えなし) | - | - | - | - |"
    rows = []
    for e in flagged:
        reasons = "、".join(e.get("reasons") or [])
        rows.append(
            f"| [`{e.get('asin', '')}`]({e.get('page', '')}) | {e.get('cohort', '')} | "
            f"{_fmt_pct(e.get('max_sim'))} | {_fmt_pct(e.get('centroid_sim'))} | {reasons} |"
        )
    return "\n".join(rows)


def render_comment_body(payload: dict[str, Any]) -> str:
    source_week = payload.get("source_week", "")
    model = payload.get("model", "")
    corpus_size = payload.get("corpus_size", 0)
    cohort_stats = payload.get("cohort_stats") or {}
    flagged = payload.get("flagged") or []
    flagged_total = payload.get("flagged_total")

    parts = [
        f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->",
        "## 🧭 凡庸度の定量監査 (週次ロールアップ)",
        "",
        "**検証フェーズ — この監査は自動リライトには未連動です。** "
        "K8/Ruri (canonical 埋め込みレーン) で記事 narrative 全文の類似度を測り、"
        "v7 プロンプト (#3203 Phase 1) 施行前後で記事の個性化が進んでいるか、"
        "および near-duplicate/最も平均的な記事をリライト候補選定の参考として提示します。",
        "",
        f"モデル: `{model}` / 週: `{source_week}` / コーパスサイズ: {corpus_size} 記事",
        "",
        render_threshold_line(payload),
        "",
        "### cohort 比較 (v7 施行前後の類似度分布)",
        "",
        "| cohort | 記事数 | max_sim p50 | max_sim p90 | centroid_sim p50 | centroid_sim p90 |",
        "|---|---:|---:|---:|---:|---:|",
        render_cohort_rows(cohort_stats),
        "",
        "> **all 行が週次で下がっているか**が #3203 の効果判定の主指標です。"
        "flagged は分布ベースの相対閾値なので件数はほぼ一定になります — "
        "改善の有無は上記「絶対基準での超過件数」と all 行の百分位で見てください。",
        "",
        f"### flagged 記事 (閾値超え {flagged_total if flagged_total is not None else len(flagged)} 件中、"
        f"max_sim 降順・上位{len(flagged)}件)",
        "",
        "| 記事 | cohort | max_sim | centroid_sim | reasons |",
        "|---|---|---:|---:|---|",
        render_flagged_rows(flagged),
        "",
        "> ⚠️ 類似度はローカル LLM (K8/Ruri, cl-nagoya/ruri-v3-310m) による参考値です。"
        "リライトの要否は owner の判断に委ねます。",
        "",
        f"<!-- {WEEK_MARKER_PREFIX}{source_week} -->",
    ]
    return "\n".join(parts)


def post_comment(repo: str, issue_number: int, body: str) -> None:
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "-R", repo, "--body", body],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument(
        "--issue-number", type=int, default=None,
        help=f"コメント先の tracker issue 番号を直接指定する。省略時は $TRACKER_ISSUE、"
             f"それも無ければ既定 #{DEFAULT_TRACKER_ISSUE}。0 以下を渡すと従来の "
             f"marker Search による探索にフォールバックする。",
    )
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
    source_week = payload.get("source_week", "")
    if not source_week:
        logger.error("input payload missing source_week — refusing to post ambiguous comment")
        return 2

    requested = args.issue_number
    if requested is None:
        env_val = os.environ.get("TRACKER_ISSUE")
        requested = int(env_val) if env_val else DEFAULT_TRACKER_ISSUE

    issue_number = resolve_tracker_issue_number(args.repo, requested)
    if issue_number is None:
        logger.warning(
            "no open tracker issue found (marker=%r, labels=%r); skipping — "
            "this script does not create issues (GitHub API バースト禁止規律)",
            TRACKER_MARKER, TRACKER_SEARCH_LABELS,
        )
        return 0
    logger.info("using tracker issue #%d", issue_number)

    if has_existing_week_comment(args.repo, issue_number, source_week):
        logger.info("skip (already commented for week %s): issue #%d", source_week, issue_number)
        return 0

    body = render_comment_body(payload)
    if args.dry_run:
        print(f"--- would comment on issue #{issue_number} (week {source_week}) ---")
        print(body)
        return 0

    post_comment(args.repo, issue_number, body)
    logger.info("commented on issue #%d: week %s", issue_number, source_week)
    return 0


if __name__ == "__main__":
    sys.exit(main())
