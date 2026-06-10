"""open_catalog_brand_issue.py

P3 (epic #2126): `data/analytics/catalog_brand_candidates.json` を読み、
カタログ (供給側) で「ノーブランド」に埋没した未登録ブランド候補を
単一の Issue にまとめて起票する。`open_brand_suggestion_issue.py` (A-6 /
需要側 GSC) の姉妹。

設計判断:
- 候補ごとの per-brand Issue ではなく 1 Issue にまとめる
  → 人間がまとめてレビュー → brand_taxonomy.yaml に手動 PR が安全
  (alias 誤追加は全商品ページのブランド振り分けに波及するため auto-PR は危険)
- 重複防止マーカー: `<!-- p3-catalog-brands -->` (open Issue が既にあれば skip)
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
logger = logging.getLogger("open_catalog_brand_issue")

DEFAULT_IN = "data/analytics/catalog_brand_candidates.json"
MARKER = "p3-catalog-brands"
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
    items = json.loads(res.stdout).get("items", [])
    return len(items) > 0


def render_candidate_rows(candidates: list[dict]) -> list[str]:
    rows = []
    for c in candidates:
        display = c["display"]
        cnt = c["product_count"]
        variants = ", ".join(f"`{v}`" for v in c.get("raw_variants", []))
        examples = c.get("examples") or []
        ex_str = ", ".join(
            f"[{e['asin']}](https://www.amazon.co.jp/dp/{e['asin']})"
            for e in examples[:3] if e.get("asin")
        ) or "-"
        rows.append(f"| `{display}` | {cnt} | {variants} | {ex_str} |")
    return rows


def render_yaml_hint(candidates: list[dict]) -> str:
    if not candidates:
        return "(no candidates)"
    lines = ["```yaml",
             "# data/brand_taxonomy.yaml に追加する案 (人間が実ブランドか判定):",
             ""]
    for c in candidates:
        variants = c.get("raw_variants", [])
        alias_str = ", ".join(variants)
        lines.append(f"  # {c['display']} (商品数={c['product_count']})")
        lines.append(f"  - canonical: {c['display']}")
        lines.append(f"    aliases: [{alias_str}]")
        lines.append("    tier: D  # 仮、調査後に S/A/B/C/D を決定")
        lines.append("    region: \"\"  # JP / US / EU など、不明なら空")
        lines.append("")
    lines.append("```")
    return "\n".join(lines)


def render_body(data: dict) -> str:
    params = data.get("params", {})
    candidates = data.get("candidates") or []
    total_unknown = data.get("total_unknown_products", "?")
    total_groups = data.get("total_unknown_groups", "?")
    total_rejected = data.get("total_rejected_products", 0)
    rejected_brands = params.get("rejected_brands", 0)

    parts = [
        f"<!-- {MARKER} -->",
        "親 epic: #2126 (P3: カタログ起点 供給側 unknown-brand 検出器)",
        "",
        "## 概要",
        "",
        "`data/articles/*.json` の `product.brand` を全走査し、`data/brand_taxonomy.yaml` "
        "に未登録のまま「ノーブランド」に埋没しているブランド候補を **商品数降順** で抽出した。",
        "",
        "需要側検出器 (#1356 A-6 / GSC by_query) は検索流入のあるブランドしか出せず、"
        "在庫はあるが未検索の有名メーカー (PicassoTiles / Tegu 等) が不可視だった "
        "(session 136 の課題 #3)。本検出器は供給側 (カタログ) を直接走査してこれを補完する。",
        "",
        "## 検出サマリ",
        "",
        f"- 未登録 (unknown) 商品: **{total_unknown}** / グループ (fold 統合後): {total_groups}",
        f"- min_products: {params.get('min_products')} 以上の候補のみ掲載",
        f"- 走査記事数: {params.get('articles_scanned')}",
        f"- reviewed-rejected denylist で除外: {rejected_brands} brand / "
        f"{total_rejected} 商品 (`data/brand_rejected.yaml`, #2196)",
        "",
        f"## 追加候補 ({len(candidates)} 件)",
        "",
        "| ブランド (display) | 商品数 | raw 表記ゆれ | 代表 ASIN |",
        "|---|---:|---|---|",
    ]
    parts.extend(render_candidate_rows(candidates) or ["| (none) | - | - | - |"])
    parts.extend([
        "",
        "## brand_taxonomy.yaml への追加案 (素案)",
        "",
        render_yaml_hint(candidates),
        "",
        "## アクション",
        "",
        "1. 各 display が実在の玩具ブランドか調査 (公式サイト / Amazon ブランドストア / Wikipedia)",
        "2. **実ブランド** → `data/brand_taxonomy.yaml` に追加する PR (canonical + 表記ゆれ alias)。"
        "次回 full rebuild (02-publish) で `/brands/<canonical>/` が自動生成される (build_post L1994)",
        "3. **中華無名・真の不明 / 同名別業種 / カテゴリ外** (Ghbunuz / MOSYULO 等) → "
        "`data/brand_rejected.yaml` に追記 (#2196)。次回 cron 以降この候補は除外され、"
        "同じ顔ぶれの再起票が止まる (取消は当該行を消すだけ)",
        "",
        "## 自動運用",
        "",
        f"- マーカー `<!-- {MARKER} -->` で重複起票防止 (open 状態が 1 件あれば新規起票なし)",
        "- この Issue を close すると、次回 cron で残りの候補を含む新 Issue が起票される",
        "- 登録済みブランドは normalize で除外されるため、対応した分は次回自動で消える",
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

    if not args.dry_run and has_open_issue(args.repo):
        logger.info("an open catalog-brand Issue already exists — skipping create")
        return 0

    title = f"[P3] カタログ未登録ブランド候補 {len(candidates)} 件 (供給側検出)"
    body = render_body(data)

    if args.dry_run:
        logger.info("would create: %s", title)
        out = pathlib.Path("_catalog_brand_issue_preview.md")
        out.write_text(body, encoding="utf-8")
        logger.info("body preview written to %s", out)
        return 0

    url = create_issue(args.repo, title, body)
    logger.info("created: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
