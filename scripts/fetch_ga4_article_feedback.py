"""fetch_ga4_article_feedback.py

GA4 Data API から `article_feedback` カスタムイベント (epic #1365 Layer 3-②,
hugo/assets/js/article_feedback.js が送出) を月次窓で取得し、
`page_path` (event parameter, GA4 が全イベントに自動付与する page_location 由来の
`pagePath` 標準ディメンションで取得可能) × `rating` (event parameter, GA4 admin で
event-scoped custom dimension として登録しないと `customEvent:rating` を
dimension として query できない) で集計し、
`data/analytics/article_feedback_monthly.json` に書き出す read-only スクリプト。

設計方針:
- 副作用ゼロ (score / narrative / 記事生成パイプラインに一切影響しない)
- 認証・CLI引数は scripts/fetch_ga4.py と揃える (SA JSON / property id)
- `customEvent:rating` は GA4 admin で custom dimension 未登録だと 400 で
  失敗する。失敗時は `pagePath` のみ (rating 内訳なし、イベント総数のみ) に
  fallback し、`rating_dimension_available: false` を出力に残す
  (issue #2051 に明記された既知の制約)。
- eventName フィルタで `article_feedback` のみに絞る。hostName フィルタは
  付けない — この widget は hugo/layouts/partials/extend_footer.html の
  posts セクション限定で mount されており (omcha.jp = WordPress 側には
  script 自体が読み込まれない)、navi.omcha.jp 以外から発生し得ないため。

Issue: https://github.com/omochairo/amazon/issues/2051
  (親 epic #1365 Layer 3-② follow-up)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_ga4_article_feedback")

DEFAULT_OUT = "data/analytics/article_feedback_monthly.json"
DEFAULT_DAYS = 30
EVENT_NAME = "article_feedback"
RATING_DIMENSION = "customEvent:rating"


def _month_range(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> (月初 ISO日付, 月末 ISO日付) の pure function。"""
    year_s, mon_s = month.split("-", 1)
    year, mon = int(year_s), int(mon_s)
    start = date(year, mon, 1)
    if mon == 12:
        next_start = date(year + 1, 1, 1)
    else:
        next_start = date(year, mon + 1, 1)
    end = next_start - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _resolve_range(*, days: int, end_date: str | None, month: str | None) -> tuple[str, str]:
    """--month 指定時は暦月優先、なければ従来の --days/--end-date 窓。"""
    if month:
        return _month_range(month)
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _load_credentials(sa_json: str):
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )


def _event_name_filter():
    from google.analytics.data_v1beta.types import Filter, FilterExpression
    return FilterExpression(
        filter=Filter(
            field_name="eventName",
            string_filter=Filter.StringFilter(
                value=EVENT_NAME,
                match_type=Filter.StringFilter.MatchType.EXACT,
            ),
        )
    )


def _run_report(client, property_id: str, start: str, end: str,
                 dims: list[str], metrics: list[str], limit: int = 10000) -> list[dict]:
    """1 リクエスト = 1 dim 組み合わせ。rows を dict 列に正規化して返す。

    scripts/fetch_ga4.py の同名関数と同型 (dimension_filter は常に eventName 固定
    のためこちらでは常時付与し、引数からは外している)。
    """
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )
    req = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimension_filter=_event_name_filter(),
        limit=limit,
    )
    res = client.run_report(req)
    rows = []
    for r in res.rows:
        row = {dims[i]: r.dimension_values[i].value for i in range(len(dims))}
        for i, m in enumerate(metrics):
            v = r.metric_values[i].value
            row[m] = float(v) if "." in v or "e" in v.lower() else int(v)
        rows.append(row)
    return rows


def fetch(property_id: str, sa_json: str, *, days: int = DEFAULT_DAYS,
          end_date: str | None = None, month: str | None = None,
          top_n: int = 10000) -> dict[str, Any]:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    creds = _load_credentials(sa_json)
    client = BetaAnalyticsDataClient(credentials=creds)

    start_s, end_s = _resolve_range(days=days, end_date=end_date, month=month)
    logger.info("range: %s .. %s (property=%s)", start_s, end_s, property_id)

    rating_available = True
    rows: list[dict] = []
    try:
        rows = _run_report(
            client, property_id, start_s, end_s,
            dims=["pagePath", RATING_DIMENSION],
            metrics=["eventCount"],
            limit=top_n,
        )
    except Exception as e:  # noqa: BLE001 — GA4 400 (dimension not registered) 等
        logger.warning(
            "customEvent:rating query failed (likely GA4 custom dimension not "
            "registered yet — see issue #2051); falling back to page_path-only "
            "event counts. error: %s", e,
        )
        rating_available = False
        rows = _run_report(
            client, property_id, start_s, end_s,
            dims=["pagePath"],
            metrics=["eventCount"],
            limit=top_n,
        )

    total_events = sum(r.get("eventCount", 0) for r in rows)
    if not rating_available:
        # rating 次元なしの行は host な pagePath 単位で1行のはずなのでそのまま数える
        pass

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "property_id": property_id,
        "range": {"start": start_s, "end": end_s},
        "event_name": EVENT_NAME,
        "rating_dimension_available": rating_available,
        "totals": {
            "rows": len(rows),
            "eventCount_sum": total_events,
        },
        "rows": rows,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--property-id", default=os.environ.get("GA4_PROPERTY_ID"))
    p.add_argument("--sa-json", default=os.environ.get("GA4_SA_JSON"),
                    help="Service Account JSON 全文文字列。未指定なら --sa-json-file を見る")
    p.add_argument("--sa-json-file", help="SA JSON ファイルパス (ローカル debug 用)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--end-date", help="range終端をtodayでなく指定日 (YYYY-MM-DD) に固定")
    p.add_argument("--month", help="暦月で範囲指定 (YYYY-MM)。指定時は --days/--end-date より優先")
    p.add_argument("--top-n", type=int, default=10000)
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args()

    if not args.property_id:
        logger.error("GA4_PROPERTY_ID が未設定")
        return 2
    sa_json = args.sa_json
    if not sa_json and args.sa_json_file:
        sa_json = pathlib.Path(args.sa_json_file).read_text(encoding="utf-8")
    if not sa_json:
        logger.error("GA4_SA_JSON も --sa-json-file も未指定")
        return 2

    try:
        result = fetch(
            args.property_id, sa_json,
            days=args.days, end_date=args.end_date, month=args.month,
            top_n=args.top_n,
        )
    except Exception as e:
        logger.exception("GA4 article_feedback fetch failed: %s", e)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s (%d rows, %d total events, rating_dimension_available=%s)",
        out, result["totals"]["rows"], result["totals"]["eventCount_sum"],
        result["rating_dimension_available"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
