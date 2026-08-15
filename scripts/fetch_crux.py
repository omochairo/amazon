"""fetch_crux.py

B-1 Phase 1 (epic #1357, session 78): Google Chrome UX Report API から
origin + 主要 URL の実ユーザ Core Web Vitals (LCP/CLS/INP + FCP/TTFB) を取得し、
`data/analytics/history/crux_history.jsonl` に append する read-mostly スクリプト。

CrUX API:
- POST https://chromeuxreport.googleapis.com/v1/records:queryRecord?key=API_KEY
- 28 日 rolling window の実ユーザ集計を返す
- 認証: API key (Google Cloud Console で CrUX API を enable した key)
- quota: 150 QPM、daily generous
- 404 = データ不足 (low-traffic な URL は data なしのケース多数 — 正常系として扱う)

設計判断:
- form_factor は PHONE + DESKTOP を別行で取得 (mobile-first だが desktop も流入元)
- origin-level + top N URLs (default 3、GSC by_page 上位)
- **複数 origin 対応** (#5080 項目3)。navi と omcha.jp を 1 回の実行で回す。
  env `CRUX_ORIGIN` はカンマ / 空白区切りを受けるので、単一値のままでも動く
- JSONL 1 行 = (date, key_type, key_value, form_factor, *metrics) で flat
- idempotency: data/analytics/history/seen_dates.json の `crux` key に
  (date, origin) を marking。date だけで見ていた頃は origin ごとに 2 回叩く
  回避策が取れなかった (2 回目が丸ごと skip された)
- 月次 cron (19-cwv-monitor.yml) から呼び出される。daily run は意味なし
  (CrUX は 28 日集計、毎日 poll しても新規データほぼなし)
- Phase 2 (劣化検出 → Issue 起票) は 2 ヶ月分蓄積後の follow-up PR で実装

副作用:
- data/analytics/history/crux_history.jsonl への append
- data/analytics/history/seen_dates.json の更新
- 記事生成 / score / narrative には触れない

Issue: https://github.com/omochairo/amazon/issues/1357 (epic E2 / B-1)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_crux")

CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"

DEFAULT_HISTORY_DIR = "data/analytics/history"
CRUX_HISTORY_FILENAME = "crux_history.jsonl"
SEEN_DATES_FILENAME = "seen_dates.json"
# top URL の抽出元。旧実装は実行時中間ファイルの `data/analytics/gsc_weekly.json`
# を見ていたが、このファイルはコミットされたことが無く、19-cwv-monitor は
# fetch_gsc.py を走らせないので**常に存在せず**、top URL 経路は一度も動いて
# いなかった (#5080 項目3)。コミット済みの履歴 JSONL に向け直す。
# navi と WP で系列が分かれているが、どちらも `page` が絶対 URL なので、
# origin ごとにファイルを対応づけず「全部読んで origin で絞る」で足りる。
DEFAULT_GSC_INPUTS = (
    "data/analytics/history/gsc_by_page.jsonl",     # navi.omcha.jp
    "data/analytics/history/gsc_wp_by_page.jsonl",  # omcha.jp (WordPress 本家)
)
DEFAULT_TOP_URLS = 3
DEFAULT_FORM_FACTORS = ("PHONE", "DESKTOP")
HTTP_TIMEOUT = 30

# CrUX metric name → flat key prefix
METRIC_MAP = (
    ("largest_contentful_paint", "lcp"),
    ("cumulative_layout_shift", "cls"),
    ("interaction_to_next_paint", "inp"),
    ("first_contentful_paint", "fcp"),
    ("experimental_time_to_first_byte", "ttfb"),
)


def call_crux(api_key: str, body: dict, timeout: int = HTTP_TIMEOUT) -> dict | None:
    """CrUX API を呼ぶ。404 (データ不足) なら None を返す。"""
    url = f"{CRUX_ENDPOINT}?key={api_key}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def flatten_metrics(record: dict) -> dict[str, Any]:
    """CrUX record の metrics を flat dict に。p75 + histogram 3 bin の密度を抽出。"""
    metrics = record.get("metrics", {}) or {}
    out: dict[str, Any] = {}
    for crux_name, short in METRIC_MAP:
        m = metrics.get(crux_name)
        if not m:
            continue
        percentiles = m.get("percentiles") or {}
        p75 = percentiles.get("p75")
        if p75 is not None:
            out[f"{short}_p75"] = p75
        histogram = m.get("histogram") or []
        if len(histogram) >= 3:
            out[f"{short}_good_density"] = histogram[0].get("density", 0)
            out[f"{short}_ni_density"] = histogram[1].get("density", 0)
            out[f"{short}_poor_density"] = histogram[2].get("density", 0)
    return out


def _yyyymmdd(d: dict | None) -> str | None:
    """CrUX 戻り値の {year, month, day} を 'YYYY-MM-DD' に。"""
    if not d:
        return None
    y, m, day = d.get("year"), d.get("month"), d.get("day")
    if y is None or m is None or day is None:
        return None
    return f"{y:04d}-{m:02d}-{day:02d}"


def fetch_for_target(api_key: str, target: dict, form_factors: tuple[str, ...]) -> list[dict]:
    """target = {origin: ...} or {url: ...}。各 form_factor について 1 record を返す。
    404 (データ不足) は skip。"""
    records: list[dict] = []
    key_type = "origin" if "origin" in target else "url"
    key_value = target.get("origin") or target.get("url") or ""
    for ff in form_factors:
        body = dict(target)
        body["formFactor"] = ff
        try:
            resp = call_crux(api_key, body)
        except Exception as e:
            logger.warning("CrUX call failed for %s (%s): %s", key_value, ff, e)
            continue
        if resp is None:
            logger.info("no CrUX data: %s (form_factor=%s)", key_value, ff)
            continue
        rec = resp.get("record", {}) or {}
        cp = rec.get("collectionPeriod", {}) or {}
        records.append({
            "key_type": key_type,
            "key_value": key_value,
            "form_factor": ff,
            "collection_period_start": _yyyymmdd(cp.get("firstDate")),
            "collection_period_end": _yyyymmdd(cp.get("lastDate")),
            **flatten_metrics(rec),
        })
    return records


def normalize_origin(origin: str) -> str:
    """末尾スラッシュを落とした origin 文字列。CrUX の origin キーもこの形。"""
    return origin.strip().rstrip("/")


def parse_origins(raw: str | None) -> list[str]:
    """`CRUX_ORIGIN` / `--origin` の生値を origin のリストに (#5080 項目3)。

    単一 secret のままでも動くよう、カンマ区切りと空白区切りの両方を受ける。
    順序は維持し、重複は落とす (同じ origin を 2 回問い合わせて quota を捨てない)。
    """
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.replace(",", " ").split():
        o = normalize_origin(chunk)
        if o and o not in out:
            out.append(o)
    return out


def get_top_urls_from_gsc(paths: list[pathlib.Path], origin: str, n: int) -> list[str]:
    """GSC の by_page 履歴 JSONL から、その origin の impressions 上位 N URL を返す。

    入力は `{date, page, impressions, ...}` の行が日付ぶん積まれた履歴なので、
    まず **その origin について最新の date** に絞ってから上位を採る (全期間を
    混ぜると古い日の行が重複して順位を歪める)。`page` は絶対 URL なので、
    origin による前方一致でファイルを跨いだ振り分けもできる。
    """
    origin = normalize_origin(origin)
    rows: list[dict] = []
    for path in paths:
        if not path.exists():
            logger.info("gsc input %s not found — skipping this input", path)
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    page = r.get("page") or ""
                    # "https://navi.omcha.jp" が "https://navi.omcha.jpx" に
                    # 誤マッチしないよう、区切りまで含めて判定する
                    if page == origin or page.startswith(origin + "/"):
                        rows.append(r)
        except Exception as e:
            logger.warning("failed to read %s: %s — skipping this input", path, e)
            continue
    if not rows:
        logger.info("no gsc rows for %s — origin only", origin)
        return []
    latest = max(r.get("date") or "" for r in rows)
    rows = [r for r in rows if (r.get("date") or "") == latest]
    rows.sort(key=lambda r: r.get("impressions") or 0, reverse=True)
    urls: list[str] = []
    for r in rows:
        page = r.get("page")
        if page and page not in urls:
            urls.append(page)
        if len(urls) >= n:
            break
    logger.info("gsc top urls for %s (date=%s): %d", origin, latest, len(urls))
    return urls


def crux_is_done(seen_crux: dict, target_date: str, origin: str) -> bool:
    """その (date, origin) を既に取得済みか (#5080 項目3)。

    旧形式は `seen["crux"][date] = True` で origin を持っていなかった。単一
    origin 時代の記録なので、`True` は「その日は取得済み」= 全 origin 済みと
    みなす (二重 append を増やさない側に倒す)。新形式は date → {origin: True}。
    """
    v = seen_crux.get(target_date)
    if v is True:
        return True
    if isinstance(v, dict):
        return bool(v.get(normalize_origin(origin)))
    return False


def mark_crux_done(seen_crux: dict, target_date: str, origin: str) -> None:
    """(date, origin) を取得済みとして記録する。

    旧形式の `True` (= その日は全 origin 済み) は潰さない。dict に置き換えると
    記録していない origin が「未取得」に見えて二重 append を招く。
    """
    v = seen_crux.get(target_date)
    if v is True:
        return
    if not isinstance(v, dict):
        v = {}
        seen_crux[target_date] = v
    v[normalize_origin(origin)] = True


def load_seen(seen_path: pathlib.Path) -> dict:
    if not seen_path.exists():
        return {}
    return json.loads(seen_path.read_text(encoding="utf-8"))


def save_seen(seen_path: pathlib.Path, seen: dict) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def append_records(history_path: pathlib.Path, target_date: str, records: list[dict]) -> int:
    if not records:
        return 0
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        for r in records:
            row = {"date": target_date, **r}
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return len(records)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-key", default=os.environ.get("CRUX_API_KEY"))
    p.add_argument("--origin", nargs="+", default=None,
                   help="例: https://navi.omcha.jp https://omcha.jp "
                        "(env CRUX_ORIGIN はカンマ / 空白区切りで複数可)")
    p.add_argument("--gsc-input", nargs="+", default=list(DEFAULT_GSC_INPUTS),
                   help="top URL 抽出元の by_page 履歴 JSONL "
                        "(best-effort、なくても origin だけで動く)")
    p.add_argument("--top-urls", type=int, default=DEFAULT_TOP_URLS)
    p.add_argument("--form-factors", nargs="+", default=list(DEFAULT_FORM_FACTORS),
                   help="PHONE / DESKTOP / TABLET から選択")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--target-date", default=date.today().isoformat(),
                   help="seen_dates と JSONL date 列に書く値 (default: 今日)")
    args = p.parse_args()

    if not args.api_key:
        logger.error("CRUX_API_KEY missing (env or --api-key)")
        return 2
    # --origin を明示したときはそれを、無ければ env CRUX_ORIGIN を使う。
    # env 側は単一 secret に複数 origin を入れられるよう split する。
    origins = (parse_origins(" ".join(args.origin)) if args.origin
               else parse_origins(os.environ.get("CRUX_ORIGIN")))
    if not origins:
        logger.error("CRUX_ORIGIN / --origin missing")
        return 2

    history_dir = pathlib.Path(args.history_dir)
    seen_path = history_dir / SEEN_DATES_FILENAME
    history_path = history_dir / CRUX_HISTORY_FILENAME

    seen = load_seen(seen_path)
    seen_crux = seen.setdefault("crux", {})
    # 冪等キーは (date, origin)。date だけで見ていた頃は、origin ごとに 2 回
    # 叩くという回避策が取れなかった (2 回目が丸ごと skip されていた)。
    pending = [o for o in origins if not crux_is_done(seen_crux, args.target_date, o)]
    for o in origins:
        if o not in pending:
            logger.info("crux %s / %s already in history — skip", args.target_date, o)
    if not pending:
        logger.info("all origins already fetched for %s — nothing to do", args.target_date)
        return 0

    form_factors = tuple(args.form_factors)
    gsc_inputs = [pathlib.Path(p) for p in args.gsc_input]

    all_records: list[dict] = []
    for origin in pending:
        logger.info("fetch origin: %s", origin)
        all_records.extend(
            fetch_for_target(args.api_key, {"origin": origin}, form_factors))

        for url in get_top_urls_from_gsc(gsc_inputs, origin, args.top_urls):
            logger.info("fetch url: %s", url)
            all_records.extend(fetch_for_target(args.api_key, {"url": url}, form_factors))

    n = append_records(history_path, args.target_date, all_records)
    if n == 0:
        logger.warning("no CrUX data was returned for any target — "
                       "this is expected for low-traffic sites (will retry next month)")
    else:
        logger.info("appended %d records for %s to %s", n, args.target_date, history_path)

    # 取得を試みた origin は、CrUX がデータを返さなかった場合も含めて済みにする
    # (掲載閾値未満の origin を月内に何度も叩いても結果は変わらない)。
    for origin in pending:
        mark_crux_done(seen_crux, args.target_date, origin)
    seen["_meta"] = {
        "crux_last_fetch_utc": datetime.now(timezone.utc).isoformat(),
        **(seen.get("_meta") or {}),
    }
    save_seen(seen_path, seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
