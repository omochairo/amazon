"""open_query_intent_issues.py

A-7 (epic #1356): scripts/detect_query_intent.py の出力
(`data/analytics/query_intent.json`) を読み、主要検索意図が明確なページについて
「意図に合わせた CTA レイアウト確認 / 差し替え」提案 Issue を per-URL で起票する。

重複防止マーカー: `<!-- a7-intent:<URL> -->`
ラベル: quality,todo,analytics

build_post の実変更は含まない (epic 完了基準=副作用ゼロ)。本 Issue は per-page の
意図と推奨 CTA を提示し、build_post / テンプレ側での CTA レイアウト調整を促す。

## 商品ページは初検出の週だけ起票する (brain#111)

商品ページ (`/products/<asin>/`) は #1980 以降、build_post が query_intent.json を
読んで `cta_layout` を自動で当てる。人が直す余地が無いのに、ledger の持ち越し
(sticky_weeks) と Issue の有効期限 (14 日) の組み合わせで同じページが 2 週ごとに
起票され直し、brain#75 / #88 / #111 の 3 週続けて全件「作業なし」で閉じていた。

そこで商品ページは `first_detected` が今週 (= 初めて検出された週) のときだけ起票
する。お知らせとしての 1 回は残し、それ以降は黙る。自動適用の外にあるページ
(hub の /english-toys/ など) と navigational は従来どおり毎回起票する。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
import urllib.parse

from scripts._analytics_closed_keys import read_closed_keys
from scripts._analytics_issue_expiry import expiry_marker, expiry_note
from scripts._analytics_issue_search import find_taken_keys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("open_query_intent_issues")

DEFAULT_IN = "data/analytics/query_intent.json"
MARKER_PREFIX = "a7-intent:"
LABELS = "quality,todo,analytics"
INTENT_JA = {
    "commercial": "商業 (買い手意図)",
    "informational": "情報収集",
    "navigational": "指名 (ナビゲーショナル)",
}


AUTO_APPLIED_INTENTS = ("commercial", "informational")


def is_auto_applied(d: dict) -> bool:
    """build_post が `cta_layout` を自動で当てるページか。

    build_post._load_query_intent_map と同じ条件 (意図が commercial /
    informational) に、実際に照合される `/products/` 配下であることを足す。
    """
    path = urllib.parse.urlparse(d.get("page") or "").path.lower()
    return (path.startswith("/products/")
            and d.get("dominant_intent") in AUTO_APPLIED_INTENTS)


def should_file(d: dict, *, src_range: dict) -> bool:
    """起票するか。自動適用のページは初検出の週だけ (モジュール docstring 参照)。

    first_detected が無い・今週の終端日が分からないときは「初検出ではない」と
    読む (黙る側に倒す)。自動適用自体は Issue の有無に関係なく当たっている。
    """
    if not is_auto_applied(d):
        return True
    range_end = src_range.get("end")
    return bool(range_end) and d.get("first_detected") == range_end


def find_existing_taken(repo: str) -> set[str]:
    """label=quality,analytics の open Issue から重複防止キーを回収。

    ページングと索引ラグ対策は `_analytics_issue_search` に集約している
    (以前は 6 本が 1 ページ = 100 件打ち切りをそれぞれ手書きしていた)。
    """
    return find_taken_keys(repo, MARKER_PREFIX)


def render_breakdown(breakdown: dict) -> str:
    return "\n".join(
        f"| {INTENT_JA.get(k, k)} | {v} |" for k, v in breakdown.items()
    )


def render_query_rows(top_queries: list[dict]) -> str:
    if not top_queries:
        return "| (no per-query data) | - | - | - |"
    return "\n".join(
        f"| `{q['query']}` | {INTENT_JA.get(q['intent'], q['intent'])} | "
        f"{q['impressions']} | {q['position']:.1f} |"
        for q in top_queries
    )


def render_body(d: dict, *, src_range: dict) -> str:
    page = d["page"]
    intent = d["dominant_intent"]
    rng = f"{src_range.get('start','?')} 〜 {src_range.get('end','?')}"
    parts = [
        f"<!-- {MARKER_PREFIX}{page} -->",
        *([m] if (m := expiry_marker(src_range)) else []),
        "親 epic: #1356 (E1: GSC/GA4 駆動の自動最適化ループ) / 関連: #1301",
        "",
        "## 検索意図 概要",
        "",
        "| 指標 | 値 |",
        "|---|---|",
        f"| URL | `{page}` |",
        f"| 観察期間 | {rng} |",
        f"| 主要意図 | **{INTENT_JA.get(intent, intent)}** |",
        f"| 主要意図シェア | {d['dominant_share']*100:.1f}% |",
        f"| 合計 impressions | {d['total_impressions']} |",
        "",
        "## 意図別 impressions",
        "",
        "| 意図 | impressions |",
        "|---|---:|",
        render_breakdown(d.get("intent_breakdown", {})),
        "",
        "## 流入クエリ (意図ラベル付き)",
        "",
        "| クエリ | 意図 | impressions | position |",
        "|---|---|---:|---:|",
        render_query_rows(d.get("top_queries", [])),
        "",
        "## 推奨 CTA レイアウト",
        "",
        d["cta_recommendation"],
        "",
        "## アクション提案",
        "",
        f"このページの検索流入は **{INTENT_JA.get(intent, intent)}** が "
        f"{d['dominant_share']*100:.1f}% を占める。現状の CTA レイアウトがこの意図に"
        "合っているか確認し、ズレていれば上記推奨に寄せる (build_post / テンプレ調整は別 PR)。",
        *([
            "",
            "商品ページなので、上記推奨は次の build_post で `cta_layout` として自動で当たる。"
            "この Issue は初検出のお知らせで、同じページは以後起票しない。"
            "見るのは主要クエリの語が title にあるかくらいでよい。",
        ] if is_auto_applied(d) else []),
        "",
        "## 自動運用",
        "",
        f"- 重複起票防止のため本文に `<!-- {MARKER_PREFIX}{page} -->` マーカーを埋め込み済み",
        "- 同 URL が再検出されてもこの Issue が open なら新規起票なし",
        # 共通の expiry_note は 2 行目で「次週の run が立て直す」と書くが、
        # 自動適用の商品ページは初検出の週しか起票しないので 1 行目だけ使う。
        *(expiry[:1] + [
            "- 期限で自動 close されても立て直さない (cta_layout は Issue と関係なく当たり続ける)",
        ] if (expiry := expiry_note(src_range)) and is_auto_applied(d) else expiry),
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
    detected = data.get("detected") or []
    src_range = data.get("source_range") or {}

    if not detected:
        logger.info("no classified pages — exiting")
        return 0

    taken = find_existing_taken(args.repo)
    # 同じ run の掃除 step が閉じたぶんを引く。search 索引の更新は非同期で、
    # close 直後は open のまま返りうる。索引が追いついていれば no-op。
    just_closed = read_closed_keys(MARKER_PREFIX)
    if just_closed & taken:
        logger.info("ignoring %d key(s) closed earlier in this run",
                    len(just_closed & taken))
    taken -= just_closed
    logger.info("existing query-intent open Issues: %d", len(taken))

    created = 0
    for d in detected:
        page = d["page"]
        if page in taken:
            logger.info("skip (already open): %s", page)
            continue
        if not should_file(d, src_range=src_range):
            logger.info("skip (cta_layout auto-applied since %s): %s",
                        d.get("first_detected"), page)
            continue
        title = (f"[A-7] Query intent = {d['dominant_intent']} "
                 f"({d['dominant_share']*100:.0f}%): {page}")
        body = render_body(d, src_range=src_range)
        if args.dry_run:
            logger.info("would create: %s", title)
            continue
        u = create_issue(args.repo, title, body)
        logger.info("created: %s", u)
        created += 1

    logger.info("created %d new Issue(s)", created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
