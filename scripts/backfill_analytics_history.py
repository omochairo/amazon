"""scripts/backfill_analytics_history.py

18-analytics-daily.yml (#1357 B-0) の停止期間中に欠けた日次 GA4/GSC history を
1日ずつ個別に fetch し、data/analytics/history/*.jsonl へ append するバックフィル
専用スクリプト。

背景: fetch_ga4.fetch() / fetch_gsc.fetch() は --days N で「基準日からN日前まで」の
1レンジしか取れず、date 次元を持たない集計クエリのため、複数日の欠損を1回の呼び出しで
分解して取ることができない。本スクリプトは対象日ごとに --end-date 相当の値を渡して
1日単位で個別に fetch_ga4 / fetch_gsc を呼び出し、append_analytics_history の
process_ga4 / process_gsc (idempotent, seen_dates.json 管理) にそのまま乗せる。

GA4 と GSC は互いに独立して backfill 可能 (どちらかの credentials が無ければそちらは
skip)。個別日の fetch が例外を投げても他の日には影響させず、ログを残して続行する。
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
import time
from datetime import date, timedelta

from scripts.append_analytics_history import (
    DEFAULT_HISTORY_DIR,
    SEEN_DATES_FILENAME,
    load_seen_dates,
    process_ga4,
    process_gsc,
    save_seen_dates,
)
from scripts.fetch_ga4 import fetch as fetch_ga4
from scripts.fetch_gsc import fetch as fetch_gsc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_analytics_history")


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run(start: date, end: date, history_dir: pathlib.Path, args: argparse.Namespace) -> int:
    """[start, end] (inclusive) を1日ずつ backfill する。appendした総行数を返す。"""
    seen_path = history_dir / SEEN_DATES_FILENAME
    seen = load_seen_dates(seen_path)

    do_ga4 = bool(args.property_id and args.sa_json)
    do_gsc = bool(args.site_url and args.client_id and args.client_secret and args.refresh_token)
    if not do_ga4:
        logger.warning("GA4 credentials missing — GA4 backfill will be skipped")
    if not do_gsc:
        logger.warning("GSC credentials missing — GSC backfill will be skipped")

    dates = list(_date_range(start, end))
    total = 0
    for i, d in enumerate(dates):
        d_iso = d.isoformat()
        if do_ga4:
            try:
                ga4 = fetch_ga4(args.property_id, args.sa_json, 1, args.top_n, end_date=d_iso)
                total += process_ga4(ga4, history_dir, seen)
            except Exception:
                logger.exception("ga4 backfill failed for %s — skipping this date", d_iso)
        if do_gsc:
            try:
                gsc = fetch_gsc(args.site_url, args.client_id, args.client_secret,
                                args.refresh_token, 1, 0, end_date=d_iso)
                total += process_gsc(gsc, history_dir, seen)
            except Exception:
                logger.exception("gsc backfill failed for %s — skipping this date", d_iso)
        # 途中で中断されても直前までの日付が失われないよう都度保存
        save_seen_dates(seen_path, seen)
        if args.sleep_seconds and i < len(dates) - 1:
            time.sleep(args.sleep_seconds)

    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start-date", required=True, help="backfill開始日 (YYYY-MM-DD, inclusive)")
    p.add_argument("--end-date", required=True, help="backfill終了日 (YYYY-MM-DD, inclusive)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--sleep-seconds", type=float, default=1.0,
                   help="API呼び出し間隔 (rate limit対策)")

    p.add_argument("--property-id", default=os.environ.get("GA4_PROPERTY_ID"))
    p.add_argument("--sa-json", default=os.environ.get("GA4_SA_JSON"))
    p.add_argument("--top-n", type=int, default=100)

    p.add_argument("--site-url", default=os.environ.get("GSC_SITE_URL"))
    p.add_argument("--client-id", default=os.environ.get("GSC_OAUTH_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("GSC_OAUTH_CLIENT_SECRET"))
    p.add_argument("--refresh-token", default=os.environ.get("GSC_OAUTH_REFRESH_TOKEN"))
    args = p.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    if start > end:
        logger.error("--start-date must be <= --end-date")
        return 2

    if not (args.property_id and args.sa_json) and not (
        args.site_url and args.client_id and args.client_secret and args.refresh_token
    ):
        logger.error("Neither GA4 nor GSC credentials provided — nothing to backfill")
        return 2

    history_dir = pathlib.Path(args.history_dir)
    total = run(start, end, history_dir, args)
    logger.info("backfill done: %s..%s, total rows appended: %d",
                args.start_date, args.end_date, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
