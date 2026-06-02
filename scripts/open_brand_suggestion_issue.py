"""open_brand_suggestion_issue.py

A-6 (epic #1356): `data/analytics/brand_taxonomy_suggestions.json` を読み、
brand_taxonomy.yaml への追加候補トークンを単一の weekly Issue にまとめて起票する。

設計判断:
- 候補ごとの per-token Issue ではなく weekly 1 Issue にまとめる
  → 人間がまとめてレビュー → brand_taxonomy.yaml に手動 PR が安全
  (taxonomy alias 誤追加は 642 商品ページに波及するため auto-PR は危険)
- 重複防止マーカー: `<!-- a6-brand-suggestions -->` (URL 指定なし、単一)
  → open Issue が既にあれば skip (人間が close するまで再起票しない)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_brand_suggestion_issue")

DEFAULT_IN = "data/analytics/brand_taxonomy_suggestions.json"
MARKER = "a6-brand-suggestions"
LABELS = "quality,todo,analytics"


def has_open_suggestion_issue(repo: str) -> bool:
    query = (
        f"repo:{repo} is:issue is:open label:quality label:analytics "
        f'in:body "{MARKER}"'
    )
    res = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", f"q={query}", "-f", "per_page=10"],
        check=True, capture_output=True, text=True,
    )
    items = json.loads(res.stdout).get("items", [])
    return len(items) > 0


def render_candidate_rows(candidates: list[dict]) -> list[str]:
    rows = []
    for c in candidates:
        token = c["token"]
        imp = c["impressions_sum"]
        qc = c["query_count"]
        examples = c.get("queries") or []
        ex_str = ", ".join(f"`{q['query']}` ({q['impressions']})" for q in examples[:3])
        rows.append(f"| `{token}` | {imp} | {qc} | {ex_str} |")
    return rows


def render_yaml_diff_hint(candidates: list[dict]) -> str:
    if not candidates:
        return "(no candidates)"
    lines = ["```yaml",
             "# data/brand_taxonomy.yaml に追加する案 (人間が canonical と alias を決定):",
             ""]
    for c in candidates:
        lines.append(f"  # {c['token']} (imp={c['impressions_sum']}, queries={c['query_count']})")
        lines.append(f"  - canonical: {c['token']}")
        lines.append(f"    aliases: [{c['token']}]  # 表記ゆれがあれば足す")
        lines.append(f"    tier: D  # 仮、調査後に S/A/B/C/D を決定")
        lines.append(f"    region: \"\"  # JP / US / EU など、不明なら空")
        lines.append("")
    lines.append("```")
    return "\n".join(lines)


def render_body(data: dict) -> str:
    src_range = data.get("source_range") or {}
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    params = data.get("params", {})
    candidates = data.get("candidates") or []

    parts = [
        f"<!-- {MARKER} -->",
        f"親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301 / #1341",
        "",
        "## 概要",
        "",
        f"GSC 観察期間 ({rng}) で、検索流入はあるが `data/brand_taxonomy.yaml` に "
        "未登録のブランド候補トークンを抽出した。",
        "",
        "**自動 PR ではなく Issue 提案にしている理由**: alias 誤追加は 642 商品ページの "
        "ブランド正規化 / Hugo `/brands/` ページ振り分けに波及するため、人間判断を経由する。",
        "",
        "## 検出パラメータ",
        "",
        f"- min_impressions: {params.get('min_impressions')}",
        f"- min_token_len: {params.get('min_token_len')}",
        f"- top_n: {params.get('top_n')}",
        f"- 既存 taxonomy term 数: {params.get('taxonomy_term_count')}",
        "",
        f"## 追加候補 ({len(candidates)} 件)",
        "",
        "| token | impressions_sum | query_count | 代表クエリ (上位 3) |",
        "|---|---:|---:|---|",
    ]
    parts.extend(render_candidate_rows(candidates) or ["| (none) | - | - | - |"])
    parts.extend([
        "",
        "## brand_taxonomy.yaml への追加案 (素案)",
        "",
        render_yaml_diff_hint(candidates),
        "",
        "## アクション",
        "",
        "1. 各 token が実在のブランドか調査 (Wikipedia / 公式サイト / Amazon 出品)",
        "2. ブランドであれば `data/brand_taxonomy.yaml` の `brands:` に追加する PR を作成",
        "   - canonical は ユーザー検索表示名で決定",
        "   - aliases に表記ゆれ (英字 / カタカナ / 半角 / 大文字小文字) を網羅",
        "   - tier / region / safety_default を調査して埋める",
        "3. ブランドでない (キャラクター IP / 商品名 / 一般語) なら本 Issue をそのまま close",
        "   - 必要に応じて `suggest_brand_taxonomy_additions.py` の `GENERIC_TOKENS_STOPWORDS` に追加",
        "",
        "## 自動運用",
        "",
        f"- マーカー `<!-- {MARKER} -->` で重複起票防止 (open 状態が 1 件あれば新規起票なし)",
        "- この Issue を close すると、次回 cron で新規候補を含む新 Issue が起票される",
        "- セッション 77 で踏んだ手動運用 (#1341 で alias 3 件追加) の自動化",
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

    if not args.dry_run and has_open_suggestion_issue(args.repo):
        logger.info("an open brand-suggestion Issue already exists — skipping create")
        return 0

    src_range = data.get("source_range") or {}
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    title = f"[A-6] brand_taxonomy 追加候補 {len(candidates)} 件 ({rng})"
    body = render_body(data)

    if args.dry_run:
        logger.info("would create: %s", title)
        logger.info("body preview:\n%s", body[:500])
        return 0

    url = create_issue(args.repo, title, body)
    logger.info("created: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
