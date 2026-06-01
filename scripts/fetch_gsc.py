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
    """Search Analytics query 1 発。row を dict 列に正規化。"""
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": dims,
        "rowLimit": row_limit,
    }
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
          days: int, delay: int) -> dict[str, Any]:
    service = _build_service(client_id, client_secret, refresh_token)

    end = date.today() - timedelta(days=delay)
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    logger.info("range: %s .. %s (site=%s, %d-day delay buffer)",
                start_s, end_s, site_url, delay)

    by_query = _query(service, site_url, start_s, end_s, ["query"], TOP_QUERY_DEFAULT)
    by_query.sort(key=lambda r: r["clicks"], reverse=True)

    by_page = _query(service, site_url, start_s, end_s, ["page"], TOP_PAGE_DEFAULT)
    by_page.sort(key=lambda r: r["clicks"], reverse=True)

    by_combo = _query(service, site_url, start_s, end_s, ["query", "page"], TOP_COMBO_DEFAULT)
    by_combo.sort(key=lambda r: r["clicks"], reverse=True)

    by_device = _query(service, site_url, start_s, end_s, ["device"], 10)

    # 機会記事: 2 ページ目 (position 11-20) で impressions が多い page を抽出
    # → tiny tuning で 1 ページ目に押し上げ可能な候補
    opportunity = [
        r for r in by_page
        if 11.0 <= r.get("position", 0.0) <= 20.0 and r.get("impressions", 0) >= 50
    ]
    opportunity.sort(key=lambda r: r["impressions"], reverse=True)
    opportunity = opportunity[:30]

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "site_url": site_url,
        "range": {"start": start_s, "end": end_s, "days": days, "delay_days": delay},
        "totals": {
            "queries": len(by_query),
            "pages": len(by_page),
            "clicks_sum": sum(r["clicks"] for r in by_page),
            "impressions_sum": sum(r["impressions"] for r in by_page),
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
    p.add_argument("--out", default=DEFAULT_OUT)
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
                       args.refresh_token, args.days, args.delay)
    except Exception as e:
        logger.exception("GSC fetch failed: %s", e)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d queries, %d pages, %d total clicks, %d opportunity)",
                out, result["totals"]["queries"], result["totals"]["pages"],
                result["totals"]["clicks_sum"], len(result["opportunity_pages"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
