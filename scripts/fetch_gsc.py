"""fetch_gsc.py

Google Search Console Search Analytics API から検索クエリ / ページ別データを取得し、
`data/analytics/gsc_weekly.json` に書き出す read-only スクリプト。

設計方針:
- 副作用ゼロ (Phase 1 GA4 と同じ観察フェーズ)
- 認証は OAuth refresh token 方式。SA が UI バグで GSC に追加できないため
  ([[omochairo-ga4-gsc-sa-add-bug]])
- GSC データには 2-3 日の遅延があるため、range = (today-3 - days) .. (today-3)
- omcha.jp と navi.omcha.jp の GSC property は分離しているため、本スクリプトは
  GSC_SITE_URL で指定された 1 property を対象とする (navi 想定)

Issue: https://github.com/omochairo/amazon/issues/1316 (Phase 2)
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
logger = logging.getLogger("fetch_gsc")

DEFAULT_OUT = "data/analytics/gsc_weekly.json"
DEFAULT_DAYS = 7
DEFAULT_DELAY = 3  # GSC データ反映遅延 (日)
TOP_QUERY_DEFAULT = 100
TOP_PAGE_DEFAULT = 100
TOP_COMBO_DEFAULT = 200


def _build_service(client_id: str, client_secret: str, refresh_token: str):
    """OAuth refresh token から Search Console v1 service を構築。"""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    creds.refresh(Request())
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _query(service, site_url: str, start: str, end: str,
           dims: list[str], row_limit: int = 1000) -> list[dict]:
    """Search Analytics query 1 発。row を dict 列に正規化。

    dims が空 (dimensionless / site-wide query) の場合、"dimensions" キー自体を
    body から省略する。API が空リストを許容するかに依存しないための安全策。
    """
    body = {
        "startDate": start,
        "endDate": end,
        "rowLimit": row_limit,
    }
    if dims:
        body["dimensions"] = dims
    res = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = []
    for r in res.get("rows", []):
        row = {dims[i]: r["keys"][i] for i in range(len(dims))}
        row["clicks"] = int(r.get("clicks", 0))
        row["impressions"] = int(r.get("impressions", 0))
        row["ctr"] = float(r.get("ctr", 0.0))
        row["position"] = float(r.get("position", 0.0))
        rows.append(row)
    return rows


def fetch(site_url: str, client_id: str, client_secret: str, refresh_token: str,
          days: int, delay: int, end_date: str | None = None,
          top_query: int = TOP_QUERY_DEFAULT,
          top_page: int = TOP_PAGE_DEFAULT,
          top_combo: int = TOP_COMBO_DEFAULT) -> dict[str, Any]:
    service = _build_service(client_id, client_secret, refresh_token)

    end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=delay)
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    logger.info("range: %s .. %s (site=%s, %d-day delay buffer)",
                start_s, end_s, site_url, delay)

    by_query = _query(service, site_url, start_s, end_s, ["query"], top_query)
    by_query.sort(key=lambda r: r["clicks"], reverse=True)

    by_page = _query(service, site_url, start_s, end_s, ["page"], top_page)
    by_page.sort(key=lambda r: r["clicks"], reverse=True)

    by_combo = _query(service, site_url, start_s, end_s, ["query", "page"], top_combo)
    by_combo.sort(key=lambda r: r["clicks"], reverse=True)

    by_device = _query(service, site_url, start_s, end_s, ["device"], 10)

    # site-wide totals: dimensionless query (row_limit=1) で真のサイト全体集計を取得。
    # by_page は TOP_PAGE_DEFAULT 件で打ち切られるため clicks_sum/impressions_sum は
    # 「上位ページの合計」にすぎず、position に至っては site-wide の値がどこにも
    # 存在しなかった (#3988 B-1)。ranking loss と検索需要減を切り分けるには
    # 真のサイト全体 impressions / 平均 position が必要。
    sitewide_rows = _query(service, site_url, start_s, end_s, [], 1)
    if sitewide_rows:
        sitewide = sitewide_rows[0]
        clicks_sitewide = sitewide["clicks"]
        impressions_sitewide = sitewide["impressions"]
        ctr_sitewide = sitewide["ctr"]
        position_sitewide = sitewide["position"]
    else:
        # データなしの日を "0" と区別する。0 は「本当にクリックゼロ」と見分けが
        # つかず、position=0 は無意味な値になるため None のままにする。
        clicks_sitewide = None
        impressions_sitewide = None
        ctr_sitewide = None
        position_sitewide = None

    # 機会記事: 2 ページ目 (position 11-20) で impressions が多い page を抽出
    # → tiny tuning で 1 ページ目に押し上げ可能な候補
    opportunity = [
        r for r in by_page
        if 11.0 <= r.get("position", 0.0) <= 20.0 and r.get("impressions", 0) >= 50
    ]
    opportunity.sort(key=lambda r: r["impressions"], reverse=True)
    opportunity = opportunity[:30]

    truncated_pages = len(by_page) >= top_page
    truncated_queries = len(by_query) >= top_query
    truncated_combos = len(by_combo) >= top_combo

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "site_url": site_url,
        "range": {"start": start_s, "end": end_s, "days": days, "delay_days": delay},
        "totals": {
            "queries": len(by_query),
            "pages": len(by_page),
            # combos は by_combo (query×page) の行数。rowLimit に張り付いたかを
            # truncated_combos で見る。既定 200 のままなら従来と同じ値になる。
            "combos": len(by_combo),
            # NOTE: clicks_sum / impressions_sum は by_page (上位 TOP_PAGE_DEFAULT 件)
            # の合計であり、truncated_pages が True の場合サイト全体の値ではない。
            # 真のサイト全体値は *_sitewide を使うこと。
            "clicks_sum": sum(r["clicks"] for r in by_page),
            "impressions_sum": sum(r["impressions"] for r in by_page),
            "clicks_sitewide": clicks_sitewide,
            "impressions_sitewide": impressions_sitewide,
            "ctr_sitewide": ctr_sitewide,
            "position_sitewide": position_sitewide,
            "truncated_pages": truncated_pages,
            "truncated_queries": truncated_queries,
            "truncated_combos": truncated_combos,
        },
        "by_query": by_query,
        "by_page": by_page,
        "by_combo": by_combo,
        "by_device": by_device,
        "opportunity_pages": opportunity,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--site-url", default=os.environ.get("GSC_SITE_URL"))
    p.add_argument("--client-id", default=os.environ.get("GSC_OAUTH_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("GSC_OAUTH_CLIENT_SECRET"))
    p.add_argument("--refresh-token", default=os.environ.get("GSC_OAUTH_REFRESH_TOKEN"))
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--delay", type=int, default=DEFAULT_DELAY)
    p.add_argument("--end-date", help="range終端を(today - delay)でなく指定日 (YYYY-MM-DD) に固定 (backfill用、--delayは無視される)")
    p.add_argument("--out", default=DEFAULT_OUT)
    # rowLimit の上書き。既定値は据え置きなので既存の呼び出し (navi 日次/週次) は不変。
    # omcha.jp (832記事) のように母数が大きい property を週次窓で取るときに、
    # 既定の 100/100/200 では上位しか返らず week-over-week 比較が成立しないため
    # ([[project-omcha-ops]] の rewrite-radar)。GSC API の rowLimit 上限は 25,000。
    p.add_argument("--top-query", type=int, default=TOP_QUERY_DEFAULT,
                   help=f"by_query の rowLimit (default {TOP_QUERY_DEFAULT}, max 25000)")
    p.add_argument("--top-page", type=int, default=TOP_PAGE_DEFAULT,
                   help=f"by_page の rowLimit (default {TOP_PAGE_DEFAULT}, max 25000)")
    p.add_argument("--top-combo", type=int, default=TOP_COMBO_DEFAULT,
                   help=f"by_combo (query×page) の rowLimit (default {TOP_COMBO_DEFAULT}, max 25000)")
    args = p.parse_args()

    missing = [n for n, v in [
        ("GSC_SITE_URL", args.site_url),
        ("GSC_OAUTH_CLIENT_ID", args.client_id),
        ("GSC_OAUTH_CLIENT_SECRET", args.client_secret),
        ("GSC_OAUTH_REFRESH_TOKEN", args.refresh_token),
    ] if not v]
    if missing:
        logger.error("missing required env/args: %s", ", ".join(missing))
        return 2

    try:
        result = fetch(args.site_url, args.client_id, args.client_secret,
                       args.refresh_token, args.days, args.delay, args.end_date,
                       args.top_query, args.top_page, args.top_combo)
    except Exception as e:
        logger.exception("GSC fetch failed: %s", e)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    t = result["totals"]
    logger.info(
        "wrote %s (%d queries, %d pages, %d combos, %d top-page clicks, %d opportunity; "
        "sitewide clicks=%s impressions=%s ctr=%s position=%s "
        "truncated_pages=%s truncated_queries=%s truncated_combos=%s)",
        out, t["queries"], t["pages"], t["combos"], t["clicks_sum"],
        len(result["opportunity_pages"]),
        t["clicks_sitewide"], t["impressions_sitewide"], t["ctr_sitewide"], t["position_sitewide"],
        t["truncated_pages"], t["truncated_queries"], t["truncated_combos"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
