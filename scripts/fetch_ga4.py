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
# #2710: omcha.jp/navi.omcha.jp 合算の単一レポートを TOP_N_DEFAULT で server-side
# truncate すると、omcha.jp (数百ページ・日次PV数千) の陰で navi (1桁PV) がほぼ
# 全て弾かれる。detect_orphan_pages.py の DEFAULT_TARGET_HOST と同値。
NAVI_HOST = "navi.omcha.jp"


def _load_credentials(sa_json: str):
    """SA JSON 文字列から google credentials を生成。"""
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )


def _run_report(client, property_id: str, start: str, end: str,
                dims: list[str], metrics: list[str], limit: int = 10000,
                dimension_filter=None, order_bys=None) -> list[dict]:
    """1 リクエスト = 1 dim 組み合わせ。rows を dict 列に正規化して返す。

    dimension_filter / order_bys は #2710 で追加 (navi 専用フィルタ取得 +
    limit truncation の順序保証)。省略時は従来どおりフィルタなし・API 既定順。
    """
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )
    kwargs = dict(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=start, end_date=end)],
        limit=limit,
    )
    if dimension_filter is not None:
        kwargs["dimension_filter"] = dimension_filter
    if order_bys:
        kwargs["order_bys"] = order_bys
    req = RunReportRequest(**kwargs)
    res = client.run_report(req)
    rows = []
    for r in res.rows:
        row = {dims[i]: r.dimension_values[i].value for i in range(len(dims))}
        for i, m in enumerate(metrics):
            v = r.metric_values[i].value
            row[m] = float(v) if "." in v or "e" in v.lower() else int(v)
        rows.append(row)
    return rows


def _navi_dimension_filter():
    """hostName == NAVI_HOST の EXACT match FilterExpression を構築する。"""
    from google.analytics.data_v1beta.types import Filter, FilterExpression
    return FilterExpression(
        filter=Filter(
            field_name="hostName",
            string_filter=Filter.StringFilter(
                value=NAVI_HOST,
                match_type=Filter.StringFilter.MatchType.EXACT,
            ),
        )
    )


def _metric_order_by(metric_name: str, desc: bool = True):
    """指定 metric の OrderBy を構築する。

    #2710: limit=top_n で server-side truncate する際、GA4 Data API は orderBy
    未指定時の順序を仕様上保証しない (実運用上 metric 降順に見えていたのは偶然)。
    truncation 前にソート基準を明示し「top-N」を仕様として保証する。
    """
    from google.analytics.data_v1beta.types import OrderBy
    return OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metric_name), desc=desc)


def _merge_unique_rows(base: list[dict], extra: list[dict],
                        key_fields: tuple[str, ...]) -> list[dict]:
    """extra のうち base に無い key の行だけを base に追加して返す (pure function)。

    #2710: navi 専用フィルタ取得の結果を、既存の合算 top-N 結果にマージするための
    dedup ロジック。GA4 API 呼び出しに依存しないため単体テスト可能。
    """
    existing_keys = {tuple(r.get(k, "") for k in key_fields) for r in base}
    merged = list(base)
    for r in extra:
        key = tuple(r.get(k, "") for k in key_fields)
        if key not in existing_keys:
            merged.append(r)
            existing_keys.add(key)
    return merged


def _build_entrances_by_key(rows: list[dict],
                             navi_rows: list[dict] | None = None
                             ) -> dict[tuple[str, str], int]:
    """(hostName, path) -> sessions 合計の dict を構築する (pure function)。

    navi_rows は #2710 の navi 専用フィルタ取得結果。rows (合算レポート) に
    既に同じ key が含まれる場合は二重加算を避けるため skip する。
    """
    out: dict[tuple[str, str], int] = {}
    for row in rows:
        host = row.get("hostName", "")
        path = (row.get("landingPagePlusQueryString") or "").split("?", 1)[0]
        key = (host, path)
        out[key] = out.get(key, 0) + int(row.get("sessions", 0))
    for row in navi_rows or []:
        host = row.get("hostName", "")
        path = (row.get("landingPagePlusQueryString") or "").split("?", 1)[0]
        key = (host, path)
        if key in out:
            continue
        out[key] = int(row.get("sessions", 0))
    return out


def fetch(property_id: str, sa_json: str, days: int, top_n: int,
          end_date: str | None = None) -> dict[str, Any]:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    creds = _load_credentials(sa_json)
    client = BetaAnalyticsDataClient(credentials=creds)

    end = date.fromisoformat(end_date) if end_date else date.today()
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    logger.info("range: %s .. %s (property=%s)", start_s, end_s, property_id)

    # hostName + pagePath で取得 (omcha.jp / navi.omcha.jp が同一 GA4 を共有しているため、
    # サイト識別のため hostName を必ず含める。Phase 4 score 連携時の filter にも使う)
    by_page_metrics = ["screenPageViews", "engagedSessions",
                        "averageSessionDuration", "bounceRate", "engagementRate"]
    by_page = _run_report(
        client, property_id, start_s, end_s,
        dims=["hostName", "pagePath"],
        metrics=by_page_metrics,
        limit=top_n,
        order_bys=[_metric_order_by("screenPageViews")],
    )
    # #2710: 上の合算レポートは top_n で server-side truncate されており、
    # omcha.jp (数百ページ規模) の陰で navi (1桁PV) がほぼ全て弾かれる
    # (物的証拠: ga4_pages.jsonl 全38日が例外なく total_rows=100)。
    # navi 専用の同型リクエストを追加取得し、上位N漏れの navi ページを補う。
    navi_by_page = _run_report(
        client, property_id, start_s, end_s,
        dims=["hostName", "pagePath"],
        metrics=by_page_metrics,
        limit=top_n,
        dimension_filter=_navi_dimension_filter(),
        order_bys=[_metric_order_by("screenPageViews")],
    )
    by_page = _merge_unique_rows(by_page, navi_by_page, ("hostName", "pagePath"))
    by_page.sort(key=lambda r: r.get("screenPageViews", 0), reverse=True)

    # entrances は GA4 Data API に metric として存在しない (旧 UA 由来)。
    # 「そのページがセッション開始 (着地) だった回数」は landing page 次元 + sessions で
    # 取得し、(hostName, pagePath) に合算して by_page へ join する (A-5 孤児検出が利用)。
    # landing query が失敗しても全体は止めず warning に留める
    # (detect_orphan_pages.py は entrances 欠落行を skip するため degrade only)。
    # #2710: by_page と同型の truncation があるため navi 専用フィルタも追加取得する。
    entrances_by_key: dict[tuple[str, str], int] = {}
    try:
        entrances_rows = _run_report(
            client, property_id, start_s, end_s,
            dims=["hostName", "landingPagePlusQueryString"],
            metrics=["sessions"],
            limit=top_n,
            order_bys=[_metric_order_by("sessions")],
        )
        navi_entrances_rows = _run_report(
            client, property_id, start_s, end_s,
            dims=["hostName", "landingPagePlusQueryString"],
            metrics=["sessions"],
            limit=top_n,
            dimension_filter=_navi_dimension_filter(),
            order_bys=[_metric_order_by("sessions")],
        )
        entrances_by_key = _build_entrances_by_key(entrances_rows, navi_entrances_rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("entrances (landing page) fetch failed; A-5 orphan detection will skip: %s", e)
    for r in by_page:
        key = (r.get("hostName", ""), r.get("pagePath", ""))
        if key in entrances_by_key:
            r["entrances"] = entrances_by_key[key]

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
    p.add_argument("--end-date", help="range終端をtodayでなく指定日 (YYYY-MM-DD) に固定 (backfill用)")
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
        result = fetch(args.property_id, sa_json, args.days, args.top_n, args.end_date)
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
