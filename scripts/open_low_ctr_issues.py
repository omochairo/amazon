"""open_low_ctr_issues.py

A-1 (epic #1356): scripts/detect_low_ctr_pages.py の出力
(`data/analytics/low_ctr_pages.json`) を読み、各 URL について Jules リライト
候補 Issue を per-URL で GitHub に起票する。

重複起票防止:
- 各 Issue 本文に `<!-- a1-low-ctr:<URL> -->` マーカーを埋め込む
- 起票前に `gh api search/issues` で同 marker を含む open Issue を検索し、
  既存があれば skip

実行環境:
- 環境変数: GH_TOKEN (gh CLI 認証), REPO (例 omochairo/amazon)
- gh CLI が PATH にあること
- 副作用は Issue 起票のみ。記事生成 / score / narrative には触れない

呼び出し元: .github/workflows/17-analytics-report.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys

from scripts._analytics_issue_expiry import expiry_marker, expiry_note
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_low_ctr_issues")

DEFAULT_IN = "data/analytics/low_ctr_pages.json"
MARKER_PREFIX = "a1-low-ctr:"
LABELS = "quality,todo,analytics"

_SEARCH_MAX_ATTEMPTS = 3
_SEARCH_RETRY_SLEEP_SECONDS = 3.0


def _search_issues(query: str, sleeper=time.sleep) -> list[dict]:
    """`gh api search/issues` を実行し items を返す。

    GitHub Search API はインデックス反映に数秒〜数十秒のラグがあり、直前の
    書き込み (issue作成/PR作成等) 直後は既存 issue が一時的にヒットしないことが
    ある (2026-07-14、amazon-home-ops の answerability-audit workflow 実運用で
    観測)。total_count=0 の場合のみ短い間隔で再試行し (真に0件のケースは
    再試行しても変わらずそのまま受け入れる)、それ以外は初回結果を返す。
    """
    items: list[dict] = []
    for attempt in range(1, _SEARCH_MAX_ATTEMPTS + 1):
        res = subprocess.run(
            ["gh", "api", "-X", "GET", "search/issues",
             "-f", f"q={query}", "-f", "per_page=100"],
            check=True, capture_output=True, text=True,
        )
        items = json.loads(res.stdout).get("items", [])
        if items or attempt == _SEARCH_MAX_ATTEMPTS:
            return items
        logger.info("search/issues returned 0 items (attempt %d/%d); retrying "
                    "in case of search index lag", attempt, _SEARCH_MAX_ATTEMPTS)
        sleeper(_SEARCH_RETRY_SLEEP_SECONDS)
    return items


def find_existing_taken_urls(repo: str, sleeper=time.sleep) -> set[str]:
    """label=quality,analytics の open Issue で marker を含むものから URL を回収。"""
    query = (
        f"repo:{repo} is:issue is:open label:quality label:analytics "
        f'in:body "{MARKER_PREFIX}"'
    )
    items = _search_issues(query, sleeper=sleeper)
    taken: set[str] = set()
    for it in items:
        body = it.get("body") or ""
        idx = body.find(MARKER_PREFIX)
        while idx >= 0:
            tail = body[idx + len(MARKER_PREFIX):]
            url = tail.split("-->", 1)[0].strip()
            if url:
                taken.add(url)
            idx = body.find(MARKER_PREFIX, idx + 1)
    return taken


def render_query_rows(top_queries: list[dict]) -> str:
    if not top_queries:
        return "| (no per-query data) | - | - | - | - |"
    return "\n".join(
        f"| `{q['query']}` | {q['impressions']} | {q['clicks']} | "
        f"{q['ctr']*100:.2f}% | {q['position']:.1f} |"
        for q in top_queries
    )


def render_body(detected: dict, *, baseline: float, threshold: float,
                src_range: dict) -> str:
    page = detected["page"]
    imp = detected["impressions"]
    clicks = detected["clicks"]
    ctr = detected["ctr"]
    pos = detected["position"]
    ratio = detected.get("ratio_to_baseline")
    ratio_s = f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "n/a"
    top_q = detected.get("top_queries") or []
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"

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
        f"| site baseline CTR | {baseline*100:.2f}% |",
        f"| ratio_to_baseline | {ratio_s} |",
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
        f"このページは page 1 (position {pos:.1f}) に出ているが CTR が site 平均の "
        f"{ratio_s}× で、スニペット / タイトル / description が機会損失を起こしている "
        "可能性が高い。",
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
    threshold = data.get("threshold_ctr", 0.0)
    src_range = data.get("source_range") or {}

    if not detected:
        logger.info("no detected pages — exiting")
        return 0

    taken = find_existing_taken_urls(args.repo)
    logger.info("existing low-CTR open Issues: %d", len(taken))

    created = 0
    for d in detected:
        page = d["page"]
        if page in taken:
            logger.info("skip (already open): %s", page)
            continue
        title = (f"[A-1] CTR low: {page} "
                 f"(imp={d['impressions']}, ctr={d['ctr']*100:.2f}%, "
                 f"pos={d['position']:.1f})")
        body = render_body(d, baseline=baseline, threshold=threshold, src_range=src_range)
        if args.dry_run:
            logger.info("would create: %s", title)
            continue
        url = create_issue(args.repo, title, body)
        logger.info("created: %s", url)
        created += 1

    logger.info("created %d new Issue(s)", created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
