"""open_article_feedback_issue.py

scripts/detect_article_feedback_worst_pages.py の出力
(`data/analytics/article_feedback_worst_pages.json`) を読み、
article_feedback 月次ワーストページ集計を **月 1 Issue** にまとめて起票する。

設計判断 (issue #2051):
- per-page ではなく月次 1 Issue にまとめる (A-6 brand_suggestion と同様の思想:
  bad 比率ワーストは相互に関連する記事群のことが多く、まとめてレビューする方が
  効率的。per-URL Issue の乱立は GitHub API バースト規律にも反する)
- 重複防止 + 「月 1 件」の強制: マーカー `<!-- article-feedback-monthly:<YYYY-MM> -->`
  に対象月を埋め込む。同月内に再実行された場合:
    - 同月マーカーの open Issue が既にあれば **新規作成せず既存 Issue にコメント追記**
    - 月が変われば新しい marker で別 Issue になる (自然に「月次」区切りが担保される)
- rating_dimension_available が false (GA4 custom dimension 未登録) の場合も
  「件数上位ページ」を参考情報として起票し、Issue 本文に GA4 admin 側の登録手順を
  明記する (実データ集計を止めない・オーナーの手動作業を明確化する)
- ラベル: quality,todo,analytics (既存の A-4/A-5/A-6 と揃える)

Issue: https://github.com/omochairo/amazon/issues/2051
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

from scripts import gh_rest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_article_feedback_issue")

DEFAULT_IN = "data/analytics/article_feedback_worst_pages.json"
MARKER_PREFIX = "article-feedback-monthly:"
LABELS = "quality,todo,analytics"


def resolve_month(source_range: dict | None) -> str:
    """source_range.end (YYYY-MM-DD) から 'YYYY-MM' を導出する pure function。
    end が無ければ実行時点 (UTC) の年月にフォールバックする。
    """
    end = (source_range or {}).get("end")
    if end:
        return str(end)[:7]
    return datetime.now(timezone.utc).date().isoformat()[:7]


def find_open_issue_number(repo: str, month: str) -> int | None:
    marker = f"{MARKER_PREFIX}{month}"
    query = (
        f"repo:{repo} is:issue is:open label:quality label:analytics "
        f'in:body "{marker}"'
    )
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=5"],
        check=True, capture_output=True, text=True,
    )
    items = json.loads(res.stdout).get("items", [])
    if not items:
        return None
    return items[0]["number"]


def render_rows(detected: list[dict], rating_available: bool) -> list[str]:
    rows = []
    for i, d in enumerate(detected, 1):
        path = d.get("page_path", "")
        total = d.get("total", 0)
        if rating_available:
            bad_ratio = d.get("bad_ratio", 0.0)
            rows.append(
                f"| {i} | `{path}` | {total} | {d.get('good', 0)} | "
                f"{d.get('ok', 0)} | {d.get('bad', 0)} | {bad_ratio * 100:.1f}% |"
            )
        else:
            rows.append(f"| {i} | `{path}` | {total} |")
    return rows


GA4_ADMIN_STEPS = "\n".join([
    "## GA4 admin 設定 (rating 内訳を有効化するために必要)",
    "",
    "GA4 Data API は event パラメータをそのまま dimension として query できない。"
    "`rating` を event-scoped custom dimension として登録すると、次回以降の集計から"
    " bad 比率でのランキングが有効になる (登録は将来分のみに適用され、過去分には"
    "遡及されない)。",
    "",
    "1. GA4 管理画面 (Admin) → プロパティ列 → **カスタム定義 (Custom definitions)**"
    " を開く",
    "2. **カスタム ディメンションを作成 (Create custom dimension)** をクリック",
    "3. ディメンション名: `rating` (表示名は任意)、範囲 (Scope): **イベント "
    "(Event)**、イベントパラメータ (Event parameter): `rating` を選択/入力して保存",
    "4. 反映まで最大 24-48h。以降の月次集計から `customEvent:rating` が"
    " dimension として query 可能になる",
    "",
])


def render_body(report: dict, *, month: str) -> str:
    marker = f"{MARKER_PREFIX}{month}"
    rating_available = bool(report.get("rating_dimension_available"))
    src_range = report.get("source_range") or {}
    rng = f"{src_range.get('start', '?')} 〜 {src_range.get('end', '?')}"
    detected = report.get("detected") or []
    totals = report.get("totals") or {}

    parts = [
        f"<!-- {marker} -->",
        "親 epic: #1365 Layer 3-② follow-up (#2051)",
        "",
        f"## article_feedback 月次集計 ({rng})",
        "",
        f"- 対象月: {month}",
        f"- フィードバック件数合計: **{totals.get('events', 0)}**",
        f"- 集計ページ数: {totals.get('pages', 0)}",
        "- rating (好評/普通/物足りない) 内訳: "
        + ("取得済み" if rating_available else "**未取得 (GA4 custom dimension 未登録)**"),
        "",
    ]

    if not rating_available:
        parts.extend([
            "> **GA4 custom dimension 未登録のため rating 内訳なし。"
            "フィードバック件数の多い順に参考表示している。**"
            " 登録手順は本 Issue 末尾「GA4 admin 設定」を参照。",
            "",
        ])

    if rating_available:
        parts.append(f"### bad (物足りない) 比率ワースト {len(detected)} ページ")
        parts.append("")
        parts.append("| # | page_path | 件数 | good | ok | bad | bad比率 |")
        parts.append("|---:|---|---:|---:|---:|---:|---:|")
    else:
        parts.append(f"### フィードバック件数 上位 {len(detected)} ページ (rating内訳なし)")
        parts.append("")
        parts.append("| # | page_path | 件数 |")
        parts.append("|---:|---|---:|")
    parts.extend(render_rows(detected, rating_available) or ["| - | (該当ページなし) | - |"])
    parts.append("")

    parts.extend([
        "## アクション提案",
        "",
        "bad 比率が高い (または rating 内訳なしの間はフィードバック件数が多い) "
        "ページから優先的にレビューし、記事内容・above-the-fold の結論先出し・"
        "図解や比較表の追加などリライト候補として検討する。",
        "",
    ])

    if not rating_available:
        parts.append(GA4_ADMIN_STEPS)

    parts.extend([
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {marker} -->` マーカーを埋め込み済み",
        "- 同月中に再実行された場合、この Issue が open ならコメント追記のみ"
        " (新規 Issue は起票しない)",
        "- 月をまたぐと新しい marker で別 Issue が起票される",
    ])
    return "\n".join(parts)


def render_comment(report: dict, *, month: str) -> str:
    marker = f"{MARKER_PREFIX}{month}"
    rating_available = bool(report.get("rating_dimension_available"))
    detected = report.get("detected") or []
    totals = report.get("totals") or {}

    lines = [
        f"<!-- {marker}:update -->",
        f"再実行による更新 (フィードバック件数合計: {totals.get('events', 0)}, "
        f"集計ページ数: {totals.get('pages', 0)})",
        "",
    ]
    if rating_available:
        lines.append("| # | page_path | 件数 | good | ok | bad | bad比率 |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|")
    else:
        lines.append("| # | page_path | 件数 |")
        lines.append("|---:|---|---:|")
    lines.extend(render_rows(detected, rating_available) or ["| - | (該当ページなし) | - |"])
    return "\n".join(lines)


def create_issue(repo: str, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "create", "-R", repo,
         "--title", title, "--label", LABELS, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def add_comment(repo: str, issue_number: int, body: str) -> str:
    """REST 経由で Issue にコメントを投稿する (`gh issue comment` の GraphQL 経路は
    GitHub App installation token で失敗するため使わない — scripts/gh_rest.py 参照)。

    `create_issue`/`gh issue create` はもともと REST を使うため無変更。
    """
    return gh_rest.post_issue_comment(repo, issue_number, body)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_IN)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2

    report = json.loads(in_path.read_text(encoding="utf-8"))
    totals = report.get("totals") or {}
    if not totals.get("events"):
        logger.info("no article_feedback events this period — exiting")
        return 0

    month = resolve_month(report.get("source_range"))
    existing = find_open_issue_number(args.repo, month)

    if existing:
        logger.info("existing open Issue for month %s: #%d", month, existing)
        body = render_comment(report, month=month)
        if args.dry_run:
            logger.info("[dry-run] would comment on existing issue #%d", existing)
            return 0
        url = add_comment(args.repo, existing, body)
        logger.info("commented on #%d: %s", existing, url)
        return 0

    title = f"[article_feedback] 月次ワーストページ集計 ({month})"
    body = render_body(report, month=month)
    if args.dry_run:
        logger.info("[dry-run] would create: %s", title)
        logger.info("body preview:\n%s", body[:800])
        return 0

    url = create_issue(args.repo, title, body)
    logger.info("created: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
