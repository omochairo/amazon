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
# Search Analytics API の 1 リクエストあたり rowLimit 上限。これを超える件数は
# startRow のページングでしか取れない (_query が面倒を見る)。
API_ROW_LIMIT_MAX = 25000


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
           dims: list[str], row_limit: int = 1000,
           aggregation_type: str | None = None) -> list[dict]:
    """Search Analytics query。row を dict 列に正規化。

    dims が空 (dimensionless / site-wide query) の場合、"dimensions" キー自体を
    body から省略する。API が空リストを許容するかに依存しないための安全策。

    **row_limit が API の 1 リクエスト上限 (25,000) を超える場合は startRow で
    ページングする。** 単発リクエストのままだと 25,000 がハードキャップになり、
    それ以上は「取れない」ではなく「黙って切られる」形で消える。実測で by_combo
    (query x page) が 1 日 23,676 行 = 上限の 95% まで来ており、上限に張り付いた
    日から先はまた生存者バイアスが混ざる (omochairo/omcha-ops#101)。
    """
    rows: list[dict] = []
    start_row = 0
    while len(rows) < row_limit:
        page_size = min(API_ROW_LIMIT_MAX, row_limit - len(rows))
        body = {
            "startDate": start,
            "endDate": end,
            "rowLimit": page_size,
            "startRow": start_row,
        }
        if dims:
            body["dimensions"] = dims
        if aggregation_type:
            body["aggregationType"] = aggregation_type
        res = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        batch = res.get("rows", [])
        for r in batch:
            row = {dims[i]: r["keys"][i] for i in range(len(dims))}
            row["clicks"] = int(r.get("clicks", 0))
            row["impressions"] = int(r.get("impressions", 0))
            row["ctr"] = float(r.get("ctr", 0.0))
            row["position"] = float(r.get("position", 0.0))
            rows.append(row)
        # 返りが要求より少なければそこで打ち止め (次ページは空)
        if len(batch) < page_size:
            break
        start_row += len(batch)
    return rows


def fetch(site_url: str, client_id: str, client_secret: str, refresh_token: str,
          days: int, delay: int, end_date: str | None = None,
          start_date: str | None = None,
          top_query: int = TOP_QUERY_DEFAULT,
          top_page: int = TOP_PAGE_DEFAULT,
          top_combo: int = TOP_COMBO_DEFAULT,
          aggregation_type: str | None = None) -> dict[str, Any]:
    service = _build_service(client_id, client_secret, refresh_token)

    end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=delay)
    # **--days は「終端から何日さかのぼるか」であって窓の日数ではない。**
    # GSC の startDate/endDate は両端を含むので、--days 1 は 2 日窓になる。
    # 日次アーカイブはこれを 1 日窓だと思って 498 日ぶん貯めており、隣り合う
    # ファイルが 1 日ずつ重なっていた (clicks は二重計上・position は 2 日平均)。
    # 既存の呼び出し (navi の日次/週次) の窓を黙って変えないため --days の意味は
    # そのままにし、**始端を明示したい呼び出しのために --start-date を足した**。
    # start == end を渡せば正真正銘の 1 日窓になる (omochairo/omcha-ops#101)。
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    if start > end:
        raise SystemExit(f"start-date ({start}) が end-date ({end}) より後です")
    start_s, end_s = start.isoformat(), end.isoformat()
    logger.info("range: %s .. %s (site=%s, %d-day delay buffer)",
                start_s, end_s, site_url, delay)

    by_query = _query(service, site_url, start_s, end_s, ["query"], top_query, aggregation_type)
    by_query.sort(key=lambda r: r["clicks"], reverse=True)

    by_page = _query(service, site_url, start_s, end_s, ["page"], top_page, aggregation_type)
    by_page.sort(key=lambda r: r["clicks"], reverse=True)

    by_combo = _query(service, site_url, start_s, end_s, ["query", "page"], top_combo, aggregation_type)
    by_combo.sort(key=lambda r: r["clicks"], reverse=True)

    by_device = _query(service, site_url, start_s, end_s, ["device"], 10, aggregation_type)

    # site-wide totals: dimensionless query (row_limit=1) で真のサイト全体集計を取得。
    # by_page は TOP_PAGE_DEFAULT 件で打ち切られるため clicks_sum/impressions_sum は
    # 「上位ページの合計」にすぎず、position に至っては site-wide の値がどこにも
    # 存在しなかった (#3988 B-1)。ranking loss と検索需要減を切り分けるには
    # 真のサイト全体 impressions / 平均 position が必要。
    sitewide_rows = _query(service, site_url, start_s, end_s, [], 1, aggregation_type)
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
        "range": {"start": start_s, "end": end_s, "days": days,
                  # 窓が実際に何日ぶんか。--days と違い両端を含む実日数
                  "window_days": (end - start).days + 1,
                  "delay_days": delay},
        # **どの集計方式で取ったファイルかを残す。** byPage と既定 (byProperty)
        # では impressions の意味が変わる (byPage はページ単位で 1 表示と数える
        # ので 3 倍前後になる)。混ざったまま時系列にすると段差が出るが、
        # ファイルを外から見ても区別がつかない (omochairo/omcha-ops#101)
        "aggregation_type": aggregation_type,
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
    # **1 日窓では query 次元が 5,000 行ちょうどで打ち切られる** (2026-09-07 実測。
    # 別々の 3 日すべてで 5,000、startRow=5000 は 0 行なのでページングでも越えられない)。
    # aggregationType=byPage を付けると外れる (同じ日で 5,751 行・端数)。
    # 代わりに impressions が「ページ単位で 1 表示」になり 3 倍前後になる。
    # clicks と position はほぼ変わらない (omochairo/omcha-ops#101)
    p.add_argument("--aggregation-type", choices=["byPage", "byProperty"],
                   help="Search Analytics の aggregationType。省略時は API 既定")
    p.add_argument("--start-date", help="range始端を明示 (YYYY-MM-DD)。--days の代わりに使う。--end-date と同じ日を渡せば 1 日窓 (両端を含むため --days 1 は 2 日窓になる)")
    p.add_argument("--out", default=DEFAULT_OUT)
    # rowLimit の上書き。既定値は据え置きなので既存の呼び出し (navi 日次/週次) は不変。
    # omcha.jp (832記事) のように母数が大きい property を週次窓で取るときに、
    # 既定の 100/100/200 では上位しか返らず week-over-week 比較が成立しないため
    # ([[project-omcha-ops]] の rewrite-radar)。GSC API は 1 リクエスト 25,000 行が上限で、
    # それ以上は _query が startRow でページングして取る。
    p.add_argument("--top-query", type=int, default=TOP_QUERY_DEFAULT,
                   help=f"by_query の rowLimit (default {TOP_QUERY_DEFAULT}。25000 超は startRow でページングする)")
    p.add_argument("--top-page", type=int, default=TOP_PAGE_DEFAULT,
                   help=f"by_page の rowLimit (default {TOP_PAGE_DEFAULT}。25000 超は startRow でページングする)")
    p.add_argument("--top-combo", type=int, default=TOP_COMBO_DEFAULT,
                   help=f"by_combo (query×page) の rowLimit (default {TOP_COMBO_DEFAULT}。25000 超は startRow でページングする)")
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
                       args.start_date,
                       args.top_query, args.top_page, args.top_combo,
                       args.aggregation_type)
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
