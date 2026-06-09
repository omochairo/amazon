"""open_toy_recall_issue.py

#2112: `data/analytics/toy_recall_candidates.json` (fetch_toy_recalls.py 出力) を読み、
当サイト掲載ブランドに該当する消費者庁リコール候補を **単一の Issue** にまとめて
起票し、人手検証 → `hugo/data/toy_safety.json` の brand_recalls への手動 backfill を促す。

設計判断 (open_catalog_brand_issue.py の姉妹):
- 候補は自動投入しない。偽『この商品がリコール』は E-E-A-T を破壊するため、
  brand_recalls への反映は人間が検証して PR する (本 Issue がその worklist)。
- 重複防止マーカー `<!-- toy-recall-candidates -->` で open Issue が既にあれば skip。
- 既に検証済 (brand_recalls 登録済) の rcl は fetch 側で除外済 → 対応分は自然に消える。
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import pathlib
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_toy_recall_issue")

DEFAULT_IN = "data/analytics/toy_recall_candidates.json"
MARKER = "toy-recall-candidates"
LABELS = "quality,todo"


def has_open_issue(repo: str) -> bool:
    query = (
        f"repo:{repo} is:issue is:open label:quality label:todo "
        f'in:body "{MARKER}"'
    )
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=10"],
        check=True, capture_output=True, text=True,
    )
    return len(json.loads(res.stdout).get("items", [])) > 0


def render_body(data: dict) -> str:
    candidates = data.get("candidates") or []
    by_brand: dict[str, list[dict]] = collections.defaultdict(list)
    for c in candidates:
        by_brand[c["brand"]].append(c)
    # ブランドは件数降順 → 各ブランド内は新しい順
    brands_sorted = sorted(by_brand.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    parts = [
        f"<!-- {MARKER} -->",
        "親 issue: #2112 / epic: #1358 (B-5 リコール DB)",
        "",
        "## 概要",
        "",
        f"消費者庁リコール情報サイト「こども向け商品一覧」を走査し、当サイト掲載ブランド "
        f"(`data/brand_taxonomy.yaml`) に該当するリコール **{len(candidates)} 件** を抽出した "
        f"(走査 {data.get('rows_scanned', '?')} 件 / 検証済除外 {data.get('verified_excluded', 0)} 件)。",
        "",
        "⚠ **これは候補リストであり、自動では `brand_recalls` に投入していません。** "
        "偽『この商品がリコール』は E-E-A-T を破壊するため、各事例を詳細ページで人手検証し、"
        "当サイト掲載商品に関係するものだけを `hugo/data/toy_safety.json` の `brand_recalls` に手動追記してください。",
        "",
        f"出典: {data.get('source_url', '')}",
        "",
        f"## 候補 ({len(candidates)} 件 — ブランド別)",
        "",
    ]
    for brand, items in brands_sorted:
        parts.append(f"### {brand} ({len(items)})")
        parts.append("")
        parts.append("| 公示日 | カテゴリ | 商品名 | 詳細 |")
        parts.append("|---|---|---|---|")
        for c in items:
            title = c["title"].replace("|", "／")
            parts.append(
                f"| {c['post_date']} | {c['category']} | {title} | [確認]({c['url']}) |"
            )
        parts.append("")

    parts.extend([
        "## backfill 手順",
        "",
        "1. 各詳細ページを開き、**当サイトが掲載している商品 (またはその同型)** か確認する。",
        "2. 関係するものだけ `hugo/data/toy_safety.json` の `brand_recalls` に canonical ブランド名を"
        "キーとして追記する (スキーマ例):",
        "",
        "```json",
        '"brand_recalls": {',
        '  "<canonical brand>": [',
        '    {',
        '      "date": "YYYY-MM",',
        '      "product": "対象商品名 (詳細ページの表記)",',
        '      "reason": "リコール理由の要約",',
        '      "url": "https://www.recall.caa.go.jp/result/detail.php?rcl=..."',
        '    }',
        '  ]',
        '}',
        "```",
        "",
        "3. ヒット時、商品ページの安全パネルが ⚠ 注意 callout を表示する "
        "(「必ずしもこの商品が対象とは限らない」と明記済)。**掲載商品と無関係なブランド全体の"
        "リコールを安易に入れない** (例: 同ブランドでも全く別カテゴリの商品)。",
        "",
        "## 自動運用",
        "",
        f"- マーカー `<!-- {MARKER} -->` で重複起票防止 (open が 1 件あれば新規起票なし)。",
        "- backfill 済 (brand_recalls に url 登録済) の事例は次回 fetch で自動除外される。",
        "- この Issue を close すると、次回 cron で残り候補を含む新 Issue が起票される。",
    ])
    return "\n".join(parts)


def create_issue(repo: str, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "create", "-R", repo,
         "--title", title, "--label", LABELS, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


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

    data = json.loads(in_path.read_text(encoding="utf-8"))
    candidates = data.get("candidates") or []
    if not candidates:
        logger.info("no candidates — exiting")
        return 0

    title = f"[#2112] おもちゃリコール候補 {len(candidates)} 件 (要人手検証 → brand_recalls)"
    body = render_body(data)

    if args.dry_run:
        out = pathlib.Path("_toy_recall_issue_preview.md")
        out.write_text(body, encoding="utf-8")
        logger.info("would create: %s (body → %s)", title, out)
        return 0

    if has_open_issue(args.repo):
        logger.info("an open toy-recall Issue already exists — skipping create")
        return 0

    url = create_issue(args.repo, title, body)
    logger.info("created: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
