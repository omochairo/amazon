"""open_quarantine_issue.py

#2186 P2: `data/asin_quarantine.json` の `candidates` (リセラー高値の疑いで Jules が
記事化をスキップして退避した Amazon ASIN の worklist) を読み、人間レビュー用の
**単一の Issue** に起票/更新する。検証 → `data/asin_blocklist.json` 昇格 →
quarantine 除去 の運用フローを駆動する。

設計判断 (open_toy_recall_issue.py / open_catalog_brand_issue.py の姉妹):
- 自動で blocklist に昇格しない。値の偽装ブロックは E-E-A-T を破壊しうるため、
  真のリセラー転売か (中古/別商品/別SKU の誤マッチでないか) を人間が検証して PR する。
  この Issue がその worklist。
- quarantine は蓄積する worklist なので、recall/catalog と違い open Issue があれば
  **body を更新** (refresh) する。新しい退避が追記されても最新が反映される。
- 重複防止/更新先特定マーカー `<!-- asin-quarantine-candidates -->`。
- 候補 0 件なら起票しない (既存 open があっても放置 — close は人間が判断)。
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
logger = logging.getLogger("open_quarantine_issue")

DEFAULT_IN = "data/asin_quarantine.json"
MARKER = "asin-quarantine-candidates"
LABELS = "quality,todo"


def get_open_issue(repo: str) -> int | None:
    """マーカーを含む open Issue の番号を返す (無ければ None)。"""
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
    return items[0]["number"] if items else None


def render_body(data: dict) -> str:
    candidates = data.get("candidates") or []
    # ratio 降順 (転売プレミアムが高いものほど優先レビュー)
    rows = sorted(candidates, key=lambda c: -float(c.get("ratio", 0)))

    parts = [
        f"<!-- {MARKER} -->",
        "親 issue: #2186 (P2) / 元: #2185 (リセラー gate 決定木)",
        "",
        "## 概要",
        "",
        f"`check_no_reseller_pricing` が『同一新品商品なのに Amazon が cross-source 比 "
        f"1.30x 超』と判定し、Jules が記事化をスキップして "
        f"`data/asin_quarantine.json` へ退避した ASIN **{len(rows)} 件**。",
        "",
        "⚠ **これは恒久ブロックではありません。** 値の偽装を避けつつ Jules の concurrent 枠を"
        "即解放するための非ブロッキング退避です。各 ASIN を人間が検証し、"
        "**真にリセラー転売と確認できたものだけ** を `data/asin_blocklist.json` の `blocked` へ"
        "昇格してください。中古品・別商品・別SKU の誤マッチはここで除外し、当該 ASIN を"
        "quarantine から削除します (AGENTS.md §9 の決定木を参照)。",
        "",
        f"## 候補 ({len(rows)} 件 — プレミアム降順)",
        "",
        "| ASIN | Amazon | cross-src | cross 価格 | 倍率 | 退避日 | 理由 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in rows:
        asin = c.get("asin", "?")
        link = f"[{asin}](https://www.amazon.co.jp/dp/{asin})"
        reason = str(c.get("reason", "")).replace("|", "／").replace("\n", " ")
        parts.append(
            f"| {link} | ¥{c.get('amazon_price', '?')} | {c.get('cross_src', '?')} "
            f"| ¥{c.get('cross_price', '?')} | {c.get('ratio', '?')}x "
            f"| {c.get('date', '?')} | {reason} |"
        )
    parts.append("")
    parts.extend([
        "## 昇格手順",
        "",
        "1. 各 ASIN の Amazon 商品ページを開き、出品者・在庫・JAN/型番を確認する。",
        "2. **真にリセラー転売** (第三者セラーが正規品より高値、在庫薄シグナル等) と確認できたら、",
        "   `data/asin_blocklist.json` の `blocked` に `asin` / `canonical_asin` (任意) / "
        "`product_name` / `reason` / `added_at` を埋めて追記する。",
        "3. **誤マッチ** (中古・別商品・別SKU) だった場合は昇格せず、その ASIN を quarantine から削除する。",
        "4. いずれの場合も処理済 ASIN は `data/asin_quarantine.json` の `candidates` から取り除く "
        "(同 PR でまとめて可)。",
        "",
        "## 自動運用",
        "",
        f"- マーカー `<!-- {MARKER} -->` で同一 open Issue を特定。open があれば本文を更新 (refresh)。",
        "- 退避が全て処理され `candidates` が空になると、次回 cron は起票/更新を行わない。",
        "- この Issue を close しても、未処理の候補が残っていれば次回 cron で新 Issue が起票される。",
    ])
    return "\n".join(parts)


def create_issue(repo: str, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "create", "-R", repo,
         "--title", title, "--label", LABELS, "--body", body],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    res = subprocess.run(
        ["gh", "issue", "edit", str(number), "-R", repo,
         "--title", title, "--body", body],
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
        logger.info("no quarantine candidates — exiting (no issue created/updated)")
        return 0

    title = f"[#2186] リセラー退避 ASIN {len(candidates)} 件 (要人手検証 → asin_blocklist)"
    body = render_body(data)

    if args.dry_run:
        out = pathlib.Path("_quarantine_issue_preview.md")
        out.write_text(body, encoding="utf-8")
        logger.info("would upsert: %s (body -> %s)", title, out)
        return 0

    number = get_open_issue(args.repo)
    if number is not None:
        url = update_issue(args.repo, number, title, body)
        logger.info("updated #%s: %s", number, url)
    else:
        url = create_issue(args.repo, title, body)
        logger.info("created: %s", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
