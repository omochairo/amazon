"""open_low_ctr_issues.py

A-1 (epic #1356): scripts/detect_low_ctr_pages.py の出力
(`data/analytics/low_ctr_pages.json`) を読み、各 URL について Jules リライト
候補 Issue を per-URL で GitHub に起票する。

重複起票防止:
- 各 Issue 本文に `<!-- a1-low-ctr:<URL> -->` マーカーを埋め込む
- 起票前に `gh api search/issues` で同 marker を含む open Issue を検索し、
  既存があれば skip

レーン間の重複抑制 (amazon-navi-brain#33 §5):
- A-3 (query cannibalization, `<!-- a3-cannibal:<query> -->`) が同じ URL を
  既に open Issue で扱っている場合、A-1 の起票を抑制する。カニバリ解消で
  CTR 側も動きうるため、同じ URL を 2 レーンが別々に追いかけると互いを
  打ち消す
- 抑制した旨は該当 A-3 Issue にコメントで残す (`<!-- a1-suppressed:<URL> -->`
  マーカーで多重コメントを防止)。黙って捨てず、表に残す

実行環境:
- 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon)
- gh CLI が PATH にあること
- 副作用は Issue 起票と、抑制時の A-3 Issue へのコメントのみ。記事生成 /
  score / narrative には触れない

呼び出し元: .github/workflows/17-analytics-report.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time

from scripts._analytics_closed_keys import read_closed_keys
from scripts._analytics_issue_expiry import expiry_marker, expiry_note
from scripts._analytics_issue_search import find_taken_keys, search_issues

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_low_ctr_issues")

DEFAULT_IN = "data/analytics/low_ctr_pages.json"
MARKER_PREFIX = "a1-low-ctr:"
A3_MARKER_PREFIX = "a3-cannibal:"
SUPPRESSION_MARKER_PREFIX = "a1-suppressed:"
LABELS = "quality,todo,analytics"

def find_existing_taken_urls(repo: str, sleeper=time.sleep) -> set[str]:
    """label=quality,analytics の open Issue で marker を含むものから URL を回収。

    ページング・索引ラグ対策は `_analytics_issue_search` に集約した。`sleeper` は
    テストが再試行の待ちを潰すための注入口 (従来どおり)。
    """
    return find_taken_keys(repo, MARKER_PREFIX, sleeper=sleeper)


def find_a3_covered_urls(repo: str, sleeper=time.sleep) -> dict[str, int]:
    """open な A-3 (query cannibalization) Issue が扱っている URL -> issue 番号。

    A-3 の重複防止マーカーはクエリ単位 (`a3-cannibal:<query>`) で URL を含まない
    ため、`open_cannibalization_issues.render_page_rows()` が本文に埋め込む
    `` | `<URL>` | impressions | ... `` 形式のテーブル行を正規表現で拾う
    (amazon-navi-brain#33 §5)。同じ URL が複数の A-3 Issue に載っている場合は最初に見つかった
    ものを使う。
    """
    query = (f'repo:{repo} is:issue is:open label:quality label:analytics '
              f'in:body "{A3_MARKER_PREFIX}"')
    items = search_issues(query, sleeper=sleeper)
    covered: dict[str, int] = {}
    for it in items:
        number = it.get("number")
        body = it.get("body") or ""
        if not number:
            continue
        for url in re.findall(r"\|\s*`(https?://\S+?)`\s*\|", body):
            covered.setdefault(url, number)
    return covered


def already_noted_suppression(repo: str, issue_number: int, page: str) -> bool:
    """同じ URL の抑制コメントを A-3 Issue に既に残していないか確認する。"""
    res = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "-R", repo,
         "--json", "comments", "--jq", ".comments[].body"],
        check=True, capture_output=True, text=True,
    )
    marker = f"<!-- {SUPPRESSION_MARKER_PREFIX}{page} -->"
    return marker in res.stdout


def comment_suppression(repo: str, issue_number: int, page: str, detected: dict) -> None:
    """A-1 起票を抑制した旨を該当 A-3 Issue にコメントで残す。"""
    body = "\n".join([
        f"<!-- {SUPPRESSION_MARKER_PREFIX}{page} -->",
        f"[A-1] 同一 URL `{page}` が低 CTR (imp={detected['impressions']}, "
        f"ctr={detected['ctr']*100:.2f}%, pos={detected['position']:.1f}) として"
        "再検出されましたが、この Issue が同じ URL を扱っているため A-1 の"
        "個別起票は抑制しました (amazon-navi-brain#33 §5: カニバリ解消で CTR 側も動きうるため、"
        "同じ URL を 2 レーンで別々に追いかけない)。",
    ])
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "-R", repo, "--body", body],
        check=True, capture_output=True, text=True,
    )


def render_query_rows(top_queries: list[dict]) -> str:
    if not top_queries:
        return "| (no per-query data) | - | - | - | - |"
    return "\n".join(
        f"| `{q['query']}` | {q['impressions']} | {q['clicks']} | "
        f"{q['ctr']*100:.2f}% | {q['position']:.1f} |"
        for q in top_queries
    )


def render_body(detected: dict, *, baseline: float, threshold: float,
                src_range: dict, baseline_source: str = "sitewide") -> str:
    page = detected["page"]
    imp = detected["impressions"]
    clicks = detected["clicks"]
    ctr = detected["ctr"]
    pos = detected["position"]
    ratio = detected.get("ratio_to_baseline")
    ratio_s = f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "n/a"
    top_q = detected.get("top_queries") or []
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    source_note = ("実測値" if baseline_source == "sitewide"
                    else "旧スナップショットのため top-N ページ合計で代用 (amazon-navi-brain#33)")

    parts = [
        f"<!-- {MARKER_PREFIX}{page} -->",
        *([m] if (m := expiry_marker(src_range)) else []),
        f"親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## 検出概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{page}` |",
        f"| 観察期間 | {rng} |",
        f"| impressions | {imp} |",
        f"| clicks | {clicks} |",
        f"| CTR | {ctr*100:.2f}% |",
        f"| avg position | {pos:.1f} |",
        f"| site 全体 CTR (baseline) | {baseline*100:.2f}%（{source_note}） |",
        f"| このページの CTR ÷ site 全体 CTR | {ratio_s} |",
        f"| 判定しきい値 (threshold) | {threshold*100:.2f}% |",
        "",
        f"## 検索流入クエリ Top {len(top_q)}",
        "",
        "| クエリ | impressions | clicks | CTR | position |",
        "|---|---:|---:|---:|---:|",
        render_query_rows(top_q),
        "",
        "## アクション提案",
        "",
        f"このページは page 1 (position {pos:.1f}) に出ているが、CTR {ctr*100:.2f}% は"
        f"判定しきい値 {threshold*100:.2f}% を下回っている"
        f"（site 全体 CTR は {baseline*100:.2f}%。しきい値は再検出頻度を保つため "
        "baseline よりゆるく取ってあるため、このページの CTR が site 全体 CTR 以上でも"
        "検出されることがある）。スニペット / タイトル / description が機会損失を"
        "起こしている可能性がある。",
        "",
        "1. ページの `<title>` / `<meta name=description>` を上記検索クエリ語に寄せて書き直す",
        "2. 数字 / 年号 / 「2026」「最新」「徹底比較」等のクリック誘因を入れる",
        "3. emoji / 記号は控えめに",
        "4. リライト後 1-2 週間 GSC で CTR 推移を観察 → 改善なければ再起票",
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{page} -->` マーカーを埋め込み済み",
        "- 同 URL が再検出されてもこの Issue が open なら新規起票なし",
        "- close 後に再検出 → 新規 Issue で再評価",
        *expiry_note(src_range),
    ]
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
    p.add_argument("--dry-run", action="store_true",
                   help="既存検索のみ実行し、新規 Issue は作らずに想定 title だけ stdout に出す")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2

    data = json.loads(in_path.read_text(encoding="utf-8"))
    detected = data.get("detected") or []
    baseline = data.get("baseline_ctr", 0.0)
    baseline_source = data.get("baseline_source", "sitewide")
    threshold = data.get("threshold_ctr", 0.0)
    src_range = data.get("source_range") or {}

    if not detected:
        logger.info("no detected pages — exiting")
        return 0

    taken = find_existing_taken_urls(args.repo)
    # 同じ run の掃除 step が閉じたぶんを引く。search 索引の更新は非同期で、
    # close 直後は open のまま返りうる。索引が追いついていれば no-op。
    just_closed = read_closed_keys(MARKER_PREFIX)
    if just_closed & taken:
        logger.info("ignoring %d key(s) closed earlier in this run",
                    len(just_closed & taken))
    taken -= just_closed
    logger.info("existing low-CTR open Issues: %d", len(taken))

    a3_covered = find_a3_covered_urls(args.repo)
    logger.info("URLs covered by open A-3 Issues: %d", len(a3_covered))

    created = 0
    suppressed = 0
    for d in detected:
        page = d["page"]
        if page in taken:
            logger.info("skip (already open): %s", page)
            continue
        a3_issue = a3_covered.get(page)
        if a3_issue:
            logger.info("suppress (covered by A-3 #%d): %s", a3_issue, page)
            if args.dry_run:
                suppressed += 1
                continue
            if already_noted_suppression(args.repo, a3_issue, page):
                logger.info("already noted on A-3 #%d, skipping comment", a3_issue)
            else:
                comment_suppression(args.repo, a3_issue, page, d)
            suppressed += 1
            continue
        title = (f"[A-1] CTR low: {page} "
                 f"(imp={d['impressions']}, ctr={d['ctr']*100:.2f}%, "
                 f"pos={d['position']:.1f})")
        body = render_body(d, baseline=baseline, threshold=threshold,
                            src_range=src_range, baseline_source=baseline_source)
        if args.dry_run:
            logger.info("would create: %s", title)
            continue
        url = create_issue(args.repo, title, body)
        logger.info("created: %s", url)
        created += 1

    logger.info("created %d new Issue(s), suppressed %d (A-3 overlap)", created, suppressed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
