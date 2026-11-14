"""fetch_ga4_affiliate_clicks.py

購入リンクのクリック (`affiliate_click`) を、記事別・位置別に見るための GA4 取得
(navi-brain#165 T6)。read-only。

取るもの (どちらも hostName == navi.omcha.jp の完全一致で絞り、`date` で日ごとに取る):
  1. affiliate_click を date × pagePath × slot × network × asin で eventCount
  2. 下の EVENT_NAMES を date × pagePath × eventName で eventCount
     (page_view を分母に、診断の流れを数えるのに使う)

数え方: クリックは `affiliate_click` だけで数える。追従バーは押すと `m_sticky_tap` と
`affiliate_click` の両方が、セール告知は `sale_notice_click` と両方が飛ぶので、
それらを足すと二重に数えてしまう (取得 2 には参考として入れてある)。

**数字の置き場所**: このリポジトリは public。取った数字はコミットせず、
workflow 23 が private の navi-brain の issue にだけ出す。このスクリプトもログに
件数・クリック数を出さない (public の Actions のログは誰でも読める)。
`--out` は行を JSONL で書き出す (今は一時ファイル。public の history に置くと
決めたときに workflow の変更だけで済むようにしてある)。

期間: 既定は「今日 (日本時間) の 2 日前」を終わりとする直近 --days 日 (両端込み)。
当日と前日は GA4 の処理が終わっておらず数字が動くので外す。プロパティの
タイムゾーンは日本時間なので、`date` もそのまま日本時間の日付になる。

先例: scripts/fetch_ga4.py (認証・hostName の絞り方)、
scripts/fetch_ga4_article_feedback.py (customEvent:* の取り方)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_ga4_affiliate_clicks")

NAVI_HOST = "navi.omcha.jp"
JST = timezone(timedelta(hours=9))
DEFAULT_DAYS = 28
LAG_DAYS = 2
PAGE_SIZE = 10000

CLICK_EVENT = "affiliate_click"
CLICK_DIMS = ["date", "pagePath", "customEvent:slot", "customEvent:network", "customEvent:asin"]
# 行のキー名 (GA4 のディメンション名 → 出力のキー)
CLICK_KEYS = {"date": "date", "pagePath": "pagePath", "customEvent:slot": "slot",
              "customEvent:network": "network", "customEvent:asin": "asin"}

# hugo/assets/js/ が送っているイベント名 (page_view は GA4 が自動で送る)。
#   affiliate_click.js: affiliate_click / mobile_sticky_cta.js: m_sticky_tap /
#   sale_notice.js: sale_notice_click / article_feedback.js: article_feedback /
#   diagnosis.js: diagnosis_*
EVENT_NAMES = (
    "page_view",
    "affiliate_click",
    "m_sticky_tap",
    "sale_notice_click",
    "diagnosis_start",
    "diagnosis_complete",
    "diagnosis_pick_click",
    "diagnosis_more",
    "diagnosis_share",
    "diagnosis_edit",
    "diagnosis_age_best",
    "article_feedback",
)
EVENT_DIMS = ["date", "pagePath", "eventName"]
EVENT_KEYS = {"date": "date", "pagePath": "pagePath", "eventName": "eventName"}


# ---------------------------------------------------------------- 純粋関数

def today_jst(now: datetime | None = None) -> date:
    """日本時間の今日。now は tz 付きの datetime (省略時は現在時刻)。"""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(JST).date()


def resolve_window(*, start: str | None, end: str | None, days: int,
                   today: date) -> tuple[date, date]:
    """取得期間 (両端込み) を決める。

    - end 省略時は today - LAG_DAYS (当日・前日は未確定なので外す)
    - start 省略時は end から数えて days 日 (両端込み)
    """
    if days < 1:
        raise ValueError("--days は 1 以上")
    end_d = date.fromisoformat(end) if end else today - timedelta(days=LAG_DAYS)
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=days - 1)
    if start_d > end_d:
        raise ValueError(f"期間の始まり {start_d} が終わり {end_d} より後")
    return start_d, end_d


def parse_ga4_date(value: str) -> str:
    """GA4 の `date` ディメンション (YYYYMMDD) を ISO (YYYY-MM-DD) にする。

    `(other)` など日付でない値はそのまま返す (黙って落とさない)。
    """
    if len(value) == 8 and value.isdigit():
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _metric_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return int(float(value))


def rows_from_response(res, dims: list[str], keys: dict[str, str]) -> list[dict]:
    """RunReportResponse の rows を dict の列にする。metric は eventCount 1 つ。

    `(not set)` はそのまま残す (欠測を見えるようにするため)。
    """
    out = []
    for r in res.rows:
        row: dict[str, Any] = {}
        for i, d in enumerate(dims):
            v = r.dimension_values[i].value
            row[keys[d]] = parse_ga4_date(v) if d == "date" else v
        row["eventCount"] = _metric_int(r.metric_values[0].value)
        out.append(row)
    return out


def metadata_warnings(res) -> list[str]:
    """応答のメタデータから、数字が欠けている・推定であることの警告を作る。

    数値 (サンプルの件数など) は入れない。ログに出るため。
    """
    md = getattr(res, "metadata", None)
    if md is None:
        return []
    warns = []
    if getattr(md, "subject_to_thresholding", False):
        warns.append("GA4 のしきい値が適用されている (少ない行が隠れている可能性がある)")
    if list(getattr(md, "sampling_metadatas", None) or []):
        warns.append("GA4 がサンプリングしている (数字は推定値)")
    if getattr(md, "data_loss_from_other_row", False):
        warns.append("GA4 が一部の行を (other) にまとめている")
    return warns


# ---------------------------------------------------------------- リクエスト

def _string_filter(field: str, value: str):
    from google.analytics.data_v1beta.types import Filter, FilterExpression
    return FilterExpression(filter=Filter(
        field_name=field,
        string_filter=Filter.StringFilter(
            value=value, match_type=Filter.StringFilter.MatchType.EXACT,
        ),
    ))


def _in_list_filter(field: str, values: Iterable[str]):
    from google.analytics.data_v1beta.types import Filter, FilterExpression
    return FilterExpression(filter=Filter(
        field_name=field,
        in_list_filter=Filter.InListFilter(values=list(values), case_sensitive=True),
    ))


def _and(*exprs):
    from google.analytics.data_v1beta.types import FilterExpression, FilterExpressionList
    return FilterExpression(and_group=FilterExpressionList(expressions=list(exprs)))


def click_filter():
    """hostName == navi かつ eventName == affiliate_click。"""
    return _and(_string_filter("hostName", NAVI_HOST),
                _string_filter("eventName", CLICK_EVENT))


def event_filter():
    """hostName == navi かつ eventName が EVENT_NAMES のどれか。"""
    return _and(_string_filter("hostName", NAVI_HOST),
                _in_list_filter("eventName", EVENT_NAMES))


def build_request(property_id: str, start: date, end: date, dims: list[str],
                  dimension_filter, *, offset: int = 0, limit: int = PAGE_SIZE):
    """RunReportRequest を組み立てる。

    ページ送りの途中で行の順序が変わらないよう、全ディメンションで並べる
    (GA4 は order_bys が無いときの順序を保証しない)。
    """
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, OrderBy, RunReportRequest,
    )
    return RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimension_filter=dimension_filter,
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=d)) for d in dims],
        offset=offset,
        limit=limit,
    )


def run_paged(client, property_id: str, start: date, end: date, dims: list[str],
              keys: dict[str, str], dimension_filter, *,
              page_size: int = PAGE_SIZE) -> tuple[list[dict], list[str]]:
    """limit を超える分は offset を進めて取り切る。(行, 警告) を返す。"""
    rows: list[dict] = []
    warns: list[str] = []
    offset = 0
    while True:
        req = build_request(property_id, start, end, dims, dimension_filter,
                            offset=offset, limit=page_size)
        res = client.run_report(req)
        page = rows_from_response(res, dims, keys)
        rows.extend(page)
        for w in metadata_warnings(res):
            if w not in warns:
                warns.append(w)
        offset += len(page)
        total = int(getattr(res, "row_count", 0) or 0)
        if not page or offset >= total:
            break
    return rows, warns


def fetch(client, property_id: str, start: date, end: date, *,
          page_size: int = PAGE_SIZE) -> dict[str, Any]:
    clicks, w1 = run_paged(client, property_id, start, end, CLICK_DIMS, CLICK_KEYS,
                           click_filter(), page_size=page_size)
    events, w2 = run_paged(client, property_id, start, end, EVENT_DIMS, EVENT_KEYS,
                           event_filter(), page_size=page_size)
    warnings = list(dict.fromkeys(
        [f"クリックの取得: {w}" for w in w1] + [f"イベントの取得: {w}" for w in w2]))
    return {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "host": NAVI_HOST,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "warnings": warnings,
        },
        "clicks": clicks,
        "events": events,
    }


# ---------------------------------------------------------------- 書き出し

def write_jsonl(path: pathlib.Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _load_credentials(sa_json: str):
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--property-id", default=os.environ.get("GA4_PROPERTY_ID"))
    p.add_argument("--sa-json", default=os.environ.get("GA4_SA_JSON"),
                   help="Service Account JSON 全文。未指定なら --sa-json-file を見る")
    p.add_argument("--sa-json-file", help="SA JSON ファイルのパス (手元の確かめ用)")
    p.add_argument("--start", help="期間の始まり (YYYY-MM-DD・日本時間)")
    p.add_argument("--end", help="期間の終わり (YYYY-MM-DD・日本時間。既定は今日の 2 日前)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"--start が無いときの日数 (両端込み。既定 {DEFAULT_DAYS})")
    p.add_argument("--out", required=True,
                   help="行の書き出し先 (JSONL)。クリックの行とイベントの行を 1 行ずつ")
    p.add_argument("--meta-out", help="期間・警告の書き出し先 (JSON)")
    args = p.parse_args(argv)

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
        start, end = resolve_window(start=args.start, end=args.end, days=args.days,
                                    today=today_jst())
    except ValueError as e:
        logger.error("%s", e)
        return 2
    logger.info("range: %s .. %s (host=%s)", start, end, NAVI_HOST)

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        client = BetaAnalyticsDataClient(credentials=_load_credentials(sa_json))
        result = fetch(client, args.property_id, start, end)
    except Exception as e:  # noqa: BLE001
        # API のエラー文はリクエストの誤り (未登録のディメンション等) で、数字は含まない
        logger.error("GA4 の取得に失敗: %s: %s", type(e).__name__, e)
        return 1

    write_jsonl(pathlib.Path(args.out), result["clicks"] + result["events"])
    if args.meta_out:
        write_json(pathlib.Path(args.meta_out), result["meta"])
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for w in result["meta"]["warnings"]:
        logger.warning("%s", w)
        if in_actions:
            print(f"::warning::{w}")
    # 件数は出さない (public の Actions のログに残るため)
    logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
