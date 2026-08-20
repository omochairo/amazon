"""comment_quality_census.py

#4826 項目 3 のサーフェシングスクリプト。

``scripts/quality_census.py`` の出力 (``data/analytics/quality_census.json``) を、
専用の観察 issue (tracker issue) 1 本に**週次ロールアップコメント**として追記する。

なぜ専用 issue 1 本か:
  comment_uniqueness_audit.py と同じ理由。毎週新規 Issue を起票すると GitHub API
  バースト禁止規律 (CLAUDE.md 2026-07-08 制定) に反する。tracker issue を 1 本だけ
  事前に手動起票し (本文に marker `quality-census-tracker` を含める)、以後は同じ
  Issue に週次でコメントを追記する。スクリプトからの新規起票はしない
  (tracker が見つからない場合は warning + skip)。

重複コメント防止:
  各コメント本文に `<!-- quality-census:<date> -->` マーカーを埋め込み、投稿前に
  対象 Issue の既存コメントを検索して同じ date のマーカーがあれば skip する。

報告するもの (差分だけ):
  - 新規不合格 / 回復 / 継続 の件数と slug
  - check 別の不合格件数 (絶対値。上限で切らない)
  - cert_fetch=false のときは PR 時 CI との条件差を明記する

このフェーズの位置づけ:
  **観察のみ。CI を落とさないし記事も直さない。**

実行環境:
  - 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon), TRACKER_ISSUE (任意)
  - 副作用は Issue コメント追記のみ
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
logger = logging.getLogger("comment_quality_census")

DEFAULT_IN = "data/analytics/quality_census.json"
TRACKER_MARKER = "quality-census-tracker"
DATE_MARKER_PREFIX = "quality-census:"
TRACKER_SEARCH_LABELS = ("observation", "quality")

# owner が起票した恒久 tracker issue (#4828)。GitHub App installation token 経由の
# Search API は「新規作成されたばかりの issue」を数日単位で 0 件のまま返し続けることが
# あり、search/issues で毎回探す設計だと自動ロールアップが恒久的に skip され続ける
# (comment_uniqueness_audit.py の DEFAULT_TRACKER_ISSUE=3300 と同じ理由)。
# tracker は番号が固定なので Search ではなく番号直指定を既定にする。
# 0 以下を明示的に渡したときだけ marker Search にフォールバックする。
DEFAULT_TRACKER_ISSUE = 4828

# 1 コメントに列挙する slug の上限。**切り詰めた場合は必ず総数を併記する**
# (feedback-metric-gate-calibration: 上限で切ると機能不全が不可視になる)。
SLUG_LIST_LIMIT = 30


def find_tracker_issue_number(repo: str) -> int | None:
    labels = " ".join(f"label:{l}" for l in TRACKER_SEARCH_LABELS)
    query = f'repo:{repo} is:issue is:open {labels} in:body "{TRACKER_MARKER}"'
    res = gh_rest.run_gh(
        ["api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=100"],
    )
    items = json.loads(res.stdout).get("items", [])
    numbers = [it.get("number") for it in items if isinstance(it.get("number"), int)]
    return min(numbers) if numbers else None


def resolve_tracker_issue_number(repo: str, issue_number: int | None) -> int | None:
    if issue_number and issue_number > 0:
        return issue_number
    return find_tracker_issue_number(repo)


def has_existing_comment(repo: str, issue_number: int, date: str) -> bool:
    marker = f"<!-- {DATE_MARKER_PREFIX}{date} -->"
    res = gh_rest.run_gh(
        ["api", "-X", "GET", f"repos/{repo}/issues/{issue_number}/comments",
         "-f", "per_page=100"],
    )
    comments = json.loads(res.stdout)
    if not isinstance(comments, list):
        return False
    return any(marker in (c.get("body") or "") for c in comments)


def _slug_list(slugs: list[str]) -> str:
    if not slugs:
        return "なし"
    shown = slugs[:SLUG_LIST_LIMIT]
    out = ", ".join(f"`{s}`" for s in shown)
    if len(slugs) > SLUG_LIST_LIMIT:
        out += f" … 他 {len(slugs) - SLUG_LIST_LIMIT} 件 (**総数 {len(slugs)} 件**)"
    return out


def render_body(payload: dict[str, Any]) -> str:
    date = payload.get("date", "?")
    diff = payload.get("diff") or {}
    by_check = payload.get("by_check") or {}
    failing_slugs = payload.get("failing_slugs") or []
    detail = {d.get("slug"): d for d in failing_slugs if isinstance(d, dict)}

    lines: list[str] = []
    lines.append(f"<!-- {DATE_MARKER_PREFIX}{date} -->")
    lines.append(f"## quality_gate main 全量 census — {date}")
    lines.append("")
    lines.append(
        f"main 上の記事 **{payload.get('articles', 0)}** 件中、**{payload.get('failing', 0)} 件**が "
        f"現時点の quality_gate で不合格 "
        f"({(payload.get('failing_rate') or 0) * 100:.2f}%)。"
    )
    lines.append("")
    lines.append("観察のみ — CI は落とさず記事も自動修正しない。")
    lines.append("")

    prev = diff.get("previous_date")
    lines.append(f"### 前回 ({prev or '初回'}) との差分")
    lines.append("")
    lines.append("| 区分 | 件数 |")
    lines.append("|---|---|")
    lines.append(f"| 新規不合格 | {len(diff.get('new') or [])} |")
    lines.append(f"| 回復 | {len(diff.get('recovered') or [])} |")
    lines.append(f"| 継続 | {len(diff.get('persisting') or [])} |")
    lines.append("")
    lines.append(f"- **新規**: {_slug_list(list(diff.get('new') or []))}")
    lines.append(f"- **回復**: {_slug_list(list(diff.get('recovered') or []))}")
    lines.append("")

    lines.append("### check 別の不合格件数")
    lines.append("")
    if by_check:
        lines.append("| check | 件数 |")
        lines.append("|---|---|")
        for name, n in by_check.items():
            lines.append(f"| `{name}` | {n} |")
    else:
        lines.append("不合格ゼロ。")
    lines.append("")

    new_slugs = list(diff.get("new") or [])
    if new_slugs:
        lines.append("### 新規不合格の内訳")
        lines.append("")
        for s in new_slugs[:SLUG_LIST_LIMIT]:
            d = detail.get(s) or {}
            for c in d.get("failed_checks") or []:
                lines.append(f"- `{s}` — `{c.get('name')}`: {c.get('message')}")
        if len(new_slugs) > SLUG_LIST_LIMIT:
            lines.append(f"- … 他 {len(new_slugs) - SLUG_LIST_LIMIT} 件 "
                         f"(**総数 {len(new_slugs)} 件**)")
        lines.append("")

    if not payload.get("cert_fetch", False):
        lines.append(
            "> **条件差の明記**: この census は `cert_sources_content` の外部 HTTP fetch を "
            "無効にして走らせている (週次で全量 fetch すると外部負荷とタイムアウト由来の "
            "誤検出が乗るため)。PR 時の CI は fetch 有効なので、この 1 チェック分だけ "
            "条件が異なる。"
        )
        lines.append("")

    by_ded = payload.get("by_deduction") or {}
    if by_ded:
        lines.append("### 減点のみ (合否には出ない soft signal)")
        lines.append("")
        lines.append(
            "`passed=True` のままスコアだけ下がっている件数。合否だけ見ていると "
            "全件 OK に見えるが、当初設計 (指名検索 SEO = title/meta/keywords/narrative "
            "全箇所に商品名) の未達がここに出る。"
        )
        lines.append("")
        lines.append("| check | 件数 | 主な理由 |")
        lines.append("|---|---:|---|")
        reasons = payload.get("deduction_reasons") or {}
        for name, n in by_ded.items():
            top = reasons.get(name) or {}
            detail = " / ".join(f"{k} ({v})" for k, v in list(top.items())[:2]) or "-"
            lines.append(f"| `{name}` | {n} | {detail} |")
        lines.append("")

    cohorts = payload.get("cohorts") or {}
    if cohorts:
        lines.append("### 直近コホート別の減点 (施行日つき昇格の判定用)")
        lines.append("")
        lines.append(
            "上の全量集計は、施行日以降の記事がコーパスの数 % しか無い段階では "
            "**規約やプロンプト改訂の効果を原理的に表せない**。slug 順の直近 N 本で "
            "切ったものを併記する。`95%上限` は rule of three (3/N) で、**発火 0 の "
            "check にだけ意味がある**数値 (#4826 項目2 の昇格目安は 1.8% 以下)。"
        )
        lines.append("")
        lines.append("| コホート | 範囲 | 不合格 | 発火0なら95%上限 | 減点された check |")
        lines.append("|---|---|---:|---:|---|")
        for key, c in cohorts.items():
            hits = " / ".join(f"`{k}` {v}" for k, v in (c.get("by_deduction") or {}).items())
            lines.append(
                f"| `{key}` | {c.get('from')} 〜 {c.get('to')} | {c.get('failing')} "
                f"| {c.get('zero_firing_95_upper', 0):.2%} | {hits or '**なし**'} |"
            )
        lines.append("")

    md_n = payload.get("md_evaluated")
    if isinstance(md_n, int):
        total = payload.get("articles", 0)
        lines.append(
            f"> **表示面の目**: `heading_hierarchy` / `body_word_count` を実際に評価できた "
            f"(レンダリング済み Markdown があった) のは {md_n} / {total} 件。"
            + (" CI と同じく MD 無しのため、この 2 チェックは全件 pass 扱い "
               "(unknown を pass に潰す既存挙動)。" if md_n == 0 else
               " MD 有無で合否は変わらないがスコア中央値が動くため、時系列比較時は "
               "この値が同じ run 同士で比べること。")
        )
        lines.append("")

    lines.append(
        f"スコア分布: min={payload.get('score_min')} / median={payload.get('score_median')} "
        f"/ max={payload.get('score_max')}"
    )
    lines.append("")
    lines.append("_generated by `scripts/comment_quality_census.py` (#4826 項目 3)_")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument(
        "--issue-number", type=int, default=None,
        help="コメント先 tracker issue 番号。省略時は $TRACKER_ISSUE、"
             f"それも無ければ既定 #{DEFAULT_TRACKER_ISSUE}。0 以下なら marker Search。",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="投稿せず本文を標準出力に出す")
    args = p.parse_args(argv)

    path = pathlib.Path(args.input)
    if not path.exists():
        logger.warning("input not found: %s — skip", path)
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = render_body(payload)

    if args.dry_run:
        print(body)
        return 0

    if not args.repo:
        logger.error("REPO が未設定 (--repo か $REPO)")
        return 1

    issue_number = args.issue_number
    if issue_number is None:
        env = os.environ.get("TRACKER_ISSUE")
        issue_number = int(env) if env and env.strip().lstrip("-").isdigit() else DEFAULT_TRACKER_ISSUE
    issue_number = resolve_tracker_issue_number(args.repo, issue_number)
    if not issue_number:
        logger.warning(
            "tracker issue (marker %r) が見つからない — skip する。"
            "スクリプトからの新規起票はしない (GitHub API バースト禁止規律)。",
            TRACKER_MARKER,
        )
        return 0

    date = payload.get("date", "?")
    if has_existing_comment(args.repo, issue_number, date):
        logger.info("#%s に %s のコメントが既にある — skip", issue_number, date)
        return 0

    url = gh_rest.post_issue_comment(args.repo, issue_number, body)
    logger.info("posted: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
