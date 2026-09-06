"""append_analytics_history.py

B-0 (epic #1357 / session 78): GA4 / GSC の日次 fetch 結果を JSONL に append し、
analytics history を恒久蓄積する read-mostly スクリプト。

入力:
- `data/analytics/ga4_weekly.json` (fetch_ga4.py --days 1 出力を想定)
- `data/analytics/gsc_weekly.json` (fetch_gsc.py --days 1 出力を想定 / navi プロパティ)
- `data/analytics/gsc_weekly_wp.json` (--gsc-wp 指定時のみ / WP omcha.jp プロパティ)

出力 (data/analytics/history/):
- gsc_by_query.jsonl   {date, query, clicks, impressions, ctr, position}
- gsc_by_page.jsonl    {date, page, clicks, impressions, ctr, position}
- gsc_totals.jsonl     {date, clicks_sum, impressions_sum, queries_count, pages_count}
- gsc_wp_by_query.jsonl / gsc_wp_by_page.jsonl / gsc_wp_totals.jsonl
                       上記と同スキーマの WP (omcha.jp) レーン
- ga4_pages.jsonl      {date, hostName, pagePath, screenPageViews, engagedSessions,
                        averageSessionDuration, bounceRate, engagementRate}
- ga4_totals.jsonl     {date, screenPageViews_sum, engagedSessions_sum, rows_by_page}
- seen_dates.json      {gsc: {...}, gsc_wp: {...}, ga4: {...}}  (idempotency sidecar)

GSC の 2 レーン構成 (2026-07-26 追加):
  従来 GSC は navi.omcha.jp のみを計測しており、トラフィックの ~97% を占める
  WP (omcha.jp) の検索データがパイプラインに存在しなかった。そのため
  「順位が落ちたのか検索需要が減ったのか」を切り分ける手段が無い状態だった。
  既存 navi 時系列の連続性を壊さないため、同じ jsonl に混ぜず別レーンとして追加する。

設計判断:
- JSONL append-only。git diff 軽量、pandas / jq / awk で直接解析可能
- 行内 key は (date, entity) ペアで一意。seen_dates.json で per-source per-date を
  marking し、再実行 = no-op (重複 append 防止)
- 各 source の target date は入力 JSON の `range.end` を採用
  (fetch_gsc.py / fetch_ga4.py で --days 1 を指定すると range は 1 日窓になる)
- vacuum (古い date の pruning) は本スクリプトに含めない。別 PR で実装

副作用:
- data/analytics/history/*.jsonl への append
- data/analytics/history/seen_dates.json の書き換え
- 記事生成 / score / narrative には触れない
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("append_analytics_history")

DEFAULT_GA4 = "data/analytics/ga4_weekly.json"
DEFAULT_GSC = "data/analytics/gsc_weekly.json"
DEFAULT_GSC_WP = "data/analytics/gsc_weekly_wp.json"
DEFAULT_HISTORY_DIR = "data/analytics/history"
SEEN_DATES_FILENAME = "seen_dates.json"

# JSONL filenames (schema.json と同期)
GSC_BY_QUERY_FILE = "gsc_by_query.jsonl"
GSC_BY_PAGE_FILE = "gsc_by_page.jsonl"
GSC_TOTALS_FILE = "gsc_totals.jsonl"
GA4_PAGES_FILE = "ga4_pages.jsonl"
GA4_TOTALS_FILE = "ga4_totals.jsonl"

# WP (omcha.jp) 用の GSC レーン。navi 側と **別ファイル・別 seen キー** に分ける。
# 同じ jsonl に混ぜると既存の navi 時系列 (2026-06 以降の連続データ) の意味が
# 変わってしまい、#3300 等の週次比較が壊れるため。
GSC_WP_BY_QUERY_FILE = "gsc_wp_by_query.jsonl"
GSC_WP_BY_PAGE_FILE = "gsc_wp_by_page.jsonl"
GSC_WP_TOTALS_FILE = "gsc_wp_totals.jsonl"


class GscLane:
    """GSC 1 プロパティ分の出力先 (seen キー + JSONL ファイル名) をまとめたもの。"""

    __slots__ = ("seen_key", "by_query_file", "by_page_file", "totals_file", "label")

    def __init__(self, seen_key: str, by_query_file: str, by_page_file: str,
                 totals_file: str, label: str):
        self.seen_key = seen_key
        self.by_query_file = by_query_file
        self.by_page_file = by_page_file
        self.totals_file = totals_file
        self.label = label


GSC_LANE_NAVI = GscLane("gsc", GSC_BY_QUERY_FILE, GSC_BY_PAGE_FILE, GSC_TOTALS_FILE, "gsc")
GSC_LANE_WP = GscLane("gsc_wp", GSC_WP_BY_QUERY_FILE, GSC_WP_BY_PAGE_FILE,
                      GSC_WP_TOTALS_FILE, "gsc(wp)")


def load_seen_dates(seen_path: pathlib.Path) -> dict[str, dict[str, bool]]:
    if not seen_path.exists():
        return {}
    return json.loads(seen_path.read_text(encoding="utf-8"))


def save_seen_dates(seen_path: pathlib.Path, seen: dict[str, dict[str, bool]]) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return len(rows)


def _date_from_range(data: dict) -> str | None:
    rng = data.get("range") or {}
    return rng.get("end")


def process_gsc(gsc: dict, history_dir: pathlib.Path,
                seen: dict[str, dict[str, bool]],
                lane: GscLane = GSC_LANE_NAVI) -> int:
    target_date = _date_from_range(gsc)
    if not target_date:
        logger.warning("%s input has no range.end — skipping", lane.label)
        return 0
    seen_gsc = seen.setdefault(lane.seen_key, {})
    if seen_gsc.get(target_date):
        logger.info("%s date %s already in history — skip", lane.label, target_date)
        return 0

    by_query_rows = [
        {
            "date": target_date,
            "query": r.get("query", ""),
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        }
        for r in gsc.get("by_query", []) or []
    ]
    by_page_rows = [
        {
            "date": target_date,
            "page": r.get("page", ""),
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0),
            "position": r.get("position", 0.0),
        }
        for r in gsc.get("by_page", []) or []
    ]
    totals = gsc.get("totals", {}) or {}
    totals_row = {
        "date": target_date,
        "clicks_sum": totals.get("clicks_sum", 0),
        "impressions_sum": totals.get("impressions_sum", 0),
        "queries_count": totals.get("queries", 0),
        "pages_count": totals.get("pages", 0),
        # site-wide totals (#3988 B-1)。旧スキーマの入力 JSON にはキー自体が無いため
        # None / False をデフォルトにし、欠損と「本当に 0/false」を区別する
        "clicks_sitewide": totals.get("clicks_sitewide"),
        "impressions_sitewide": totals.get("impressions_sitewide"),
        "ctr_sitewide": totals.get("ctr_sitewide"),
        "position_sitewide": totals.get("position_sitewide"),
        "truncated_pages": totals.get("truncated_pages", False),
        "truncated_queries": totals.get("truncated_queries", False),
    }

    n1 = append_jsonl(history_dir / lane.by_query_file, by_query_rows)
    n2 = append_jsonl(history_dir / lane.by_page_file, by_page_rows)
    n3 = append_jsonl(history_dir / lane.totals_file, [totals_row])
    total = n1 + n2 + n3

    seen_gsc[target_date] = True
    logger.info("%s %s: appended %d rows (by_query=%d, by_page=%d, totals=%d)",
                lane.label, target_date, total, n1, n2, n3)
    return total


def process_ga4(ga4: dict, history_dir: pathlib.Path,
                seen: dict[str, dict[str, bool]]) -> int:
    target_date = _date_from_range(ga4)
    if not target_date:
        logger.warning("ga4 input has no range.end — skipping")
        return 0
    seen_ga4 = seen.setdefault("ga4", {})
    if seen_ga4.get(target_date):
        logger.info("ga4 date %s already in history — skip", target_date)
        return 0

    pages_rows = [
        {
            "date": target_date,
            "hostName": r.get("hostName", ""),
            "pagePath": r.get("pagePath", ""),
            "screenPageViews": r.get("screenPageViews", 0),
            "engagedSessions": r.get("engagedSessions", 0),
            "averageSessionDuration": r.get("averageSessionDuration", 0.0),
            "bounceRate": r.get("bounceRate", 0.0),
            # #5941 / brain#18: fetch_ga4 は by_page で engagementRate を取っているが
            # history に落ちておらず、A-4 (detect_engagement_drop) の閾値を過去データで
            # 検算できなかった。欠測は 0.0 ではなく None で残す (0.0 は「エンゲージ 0%」
            # と区別がつかず、A-4 の型の誤検出になるため。detect_engagement_drop も
            # engagementRate が無い行は skip する側に倒してある)
            "engagementRate": r.get("engagementRate"),
        }
        for r in ga4.get("by_page", []) or []
    ]
    totals = ga4.get("totals", {}) or {}
    totals_row = {
        "date": target_date,
        "screenPageViews_sum": totals.get("screenPageViews_sum", 0),
        "engagedSessions_sum": totals.get("engagedSessions_sum", 0),
        "rows_by_page": totals.get("rows_by_page", 0),
    }

    n1 = append_jsonl(history_dir / GA4_PAGES_FILE, pages_rows)
    n2 = append_jsonl(history_dir / GA4_TOTALS_FILE, [totals_row])
    total = n1 + n2

    seen_ga4[target_date] = True
    logger.info("ga4 %s: appended %d rows (pages=%d, totals=%d)",
                target_date, total, n1, n2)
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ga4", default=DEFAULT_GA4,
                   help="GA4 fetch 出力 JSON path (空文字または存在しない場合 skip)")
    p.add_argument("--gsc", default=DEFAULT_GSC,
                   help="GSC fetch 出力 JSON path / navi レーン (空文字または存在しない場合 skip)")
    p.add_argument("--gsc-wp", default="",
                   help="GSC fetch 出力 JSON path / WP (omcha.jp) レーン。"
                        "gsc_wp_*.jsonl に append する (空文字または存在しない場合 skip)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    args = p.parse_args()

    history_dir = pathlib.Path(args.history_dir)
    seen_path = history_dir / SEEN_DATES_FILENAME
    seen = load_seen_dates(seen_path)

    total_appended = 0

    ga4_path = pathlib.Path(args.ga4) if args.ga4 else None
    if ga4_path and ga4_path.exists():
        ga4 = json.loads(ga4_path.read_text(encoding="utf-8"))
        total_appended += process_ga4(ga4, history_dir, seen)
    elif args.ga4:
        logger.info("ga4 input not found: %s — skip", ga4_path)

    for arg_value, lane in ((args.gsc, GSC_LANE_NAVI), (args.gsc_wp, GSC_LANE_WP)):
        gsc_path = pathlib.Path(arg_value) if arg_value else None
        if gsc_path and gsc_path.exists():
            gsc = json.loads(gsc_path.read_text(encoding="utf-8"))
            total_appended += process_gsc(gsc, history_dir, seen, lane)
        elif arg_value:
            logger.info("%s input not found: %s — skip", lane.label, gsc_path)

    save_seen_dates(seen_path, seen)
    logger.info("done. total rows appended: %d", total_appended)
    return 0


if __name__ == "__main__":
    sys.exit(main())
