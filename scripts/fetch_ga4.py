"""fetch_ga4.py

GA4 Data API から過去 N 日 (default 7) の pagePath × 主要メトリクスを取得し、
`data/analytics/ga4_weekly.json` に書き出す read-only スクリプト。

設計方針:
- 副作用ゼロ (score / narrative / 記事生成パイプラインに一切影響しない)
- 認証は Service Account JSON。GitHub Secret `GA4_SA_JSON` に全文格納
- property は `GA4_PROPERTY_ID` (数値部分のみ、例: `460504075`)
- 失敗時は exit code 非ゼロ + ログ。出力ファイル未生成で workflow が以降の commit を skip

Issue: https://github.com/omochairo/amazon/issues/1316 (Phase 1)
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
logger = logging.getLogger("fetch_ga4")

DEFAULT_OUT = "data/analytics/ga4_weekly.json"
DEFAULT_DAYS = 7
TOP_N_DEFAULT = 100


def _load_credentials(sa_json: str):
    """SA JSON 文字列から google credentials を生成。"""
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )


def _run_report(client, property_id: str, start: str, end: str,
                dims: list[str], metrics: list[str], limit: int = 10000) -> list[dict]:
    """1 リクエスト = 1 dim 組み合わせ。rows を dict 列に正規化して返す。"""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )
    req = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=start, end_date=end)],
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


def fetch(property_id: str, sa_json: str, days: int, top_n: int) -> dict[str, Any]:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    creds = _load_credentials(sa_json)
    client = BetaAnalyticsDataClient(credentials=creds)

    end = date.today()
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    logger.info("range: %s .. %s (property=%s)", start_s, end_s, property_id)

    # hostName + pagePath で取得 (omcha.jp / navi.omcha.jp が同一 GA4 を共有しているため、
    # サイト識別のため hostName を必ず含める。Phase 4 score 連携時の filter にも使う)
    by_page = _run_report(
        client, property_id, start_s, end_s,
        dims=["hostName", "pagePath"],
        metrics=["screenPageViews", "engagedSessions",
                 "averageSessionDuration", "bounceRate", "engagementRate"],
        limit=top_n,
    )
    by_page.sort(key=lambda r: r.get("screenPageViews", 0), reverse=True)

    # サイト別合算 (cross-domain inflow/outflow の規模比較用)
    by_host = _run_report(
        client, property_id, start_s, end_s,
        dims=["hostName"],
        metrics=["screenPageViews", "engagedSessions",
                 "averageSessionDuration", "bounceRate"],
    )
    by_host.sort(key=lambda r: r.get("screenPageViews", 0), reverse=True)

    by_device = _run_report(
        client, property_id, start_s, end_s,
        dims=["deviceCategory"],
        metrics=["screenPageViews", "engagedSessions"],
    )
    by_source = _run_report(
        client, property_id, start_s, end_s,
        dims=["sessionSourceMedium"],
        metrics=["screenPageViews", "engagedSessions"],
        limit=30,
    )
    by_source.sort(key=lambda r: r.get("screenPageViews", 0), reverse=True)

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "property_id": property_id,
        "range": {"start": start_s, "end": end_s, "days": days},
        "totals": {
            "rows_by_page": len(by_page),
            "screenPageViews_sum": sum(r.get("screenPageViews", 0) for r in by_page),
            "engagedSessions_sum": sum(r.get("engagedSessions", 0) for r in by_page),
        },
        "by_host": by_host,
        "by_page": by_page,
        "by_device": by_device,
        "by_source": by_source,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--property-id", default=os.environ.get("GA4_PROPERTY_ID"))
    p.add_argument("--sa-json", default=os.environ.get("GA4_SA_JSON"),
                   help="Service Account JSON 全文文字列。未指定なら --sa-json-file を見る")
    p.add_argument("--sa-json-file", help="SA JSON ファイルパス (ローカル debug 用)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--top-n", type=int, default=TOP_N_DEFAULT)
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
        result = fetch(args.property_id, sa_json, args.days, args.top_n)
    except Exception as e:
        logger.exception("GA4 fetch failed: %s", e)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d page rows, %d total PV)",
                out, result["totals"]["rows_by_page"],
                result["totals"]["screenPageViews_sum"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
