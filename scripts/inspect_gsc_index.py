"""inspect_gsc_index.py

Google Search Console の URL Inspection API を使用して、
本番 sitemap に掲載されている製品ページ (/products/) のインデックス状況を全数調査し、
結果を集計して JSON に書き出す read-only スクリプト。

Issue: https://github.com/omochairo/amazon/issues/2701 (P4 効果測定)
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import pathlib
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inspect_gsc_index")

DEFAULT_OUT = "data/analytics/gsc_index_census.json"
DEFAULT_SITEMAP = "https://navi.omcha.jp/sitemap.xml"
DEFAULT_PREFIX = "/products/"
DEFAULT_LIMIT = 1800
# API 上限 600/分 = 10 QPS。安全側に 8 QPS (480/分) で抑える
DEFAULT_QPS = 8.0
# 1 URL あたり実測 6-7 秒かかるため、逐次だと 1500 件で 3 時間近い。
# 12 並列 = 実効 ~1.8 URL/s で 1500 件が 15 分程度に収まる (レートは QPS 側で頭打ち)
DEFAULT_WORKERS = 12
# not_indexed_urls の保持件数。0 = 無制限。
# #3331: 300 件上限だと未 index 470 件のうち 404 の 72 件中 44 件しか URL が残らず、
# 打ち手を全件に適用できなかったため既定を無制限に変更。
DEFAULT_MAX_NOT_INDEXED_URLS = 0
# #3372: 直近 N 件の API 呼び出しが連続して 429/quotaExceeded だったら「日次クォータの完全枯渇」
# とみなし、残り URL の 60s x3 リトライを打ち切る。単発の一時スロットリングとの区別は
# 「連続失敗数」で近似する (完全な区別はできないが、無駄なリトライ連打は防げる)
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 8
# Google URL Inspection API のクォータリセットは Pacific 深夜。夏時間 (PDT, UTC-7) では UTC 07:00。
# 冬時間 (PST, UTC-8) との誤差は安全側 (早め=まだリセットされていない扱い) に倒す
DEFAULT_QUOTA_RESET_HOUR_UTC = 7


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


def fetch_sitemap_urls(sitemap_url: str, prefix: str) -> list[str]:
    """Sitemap を取得してパースし、指定された prefix で始まる URL のリストを返す。"""
    logger.info("fetching sitemap: %s", sitemap_url)
    req = urllib.request.Request(
        sitemap_url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    try:
        # timeout 必須: CI で無応答時に job が無期限に張り付くのを防ぐ
        with urllib.request.urlopen(req, timeout=30) as response:
            xml_content = response.read()
    except Exception as e:
        logger.error("failed to fetch sitemap %s: %s", sitemap_url, e)
        raise

    try:
        root = ET.fromstring(xml_content)
    except Exception as e:
        logger.error("failed to parse sitemap XML: %s", e)
        raise

    ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []

    # namespace を考慮して loc 要素を検索
    for loc_elem in root.findall(".//ns:loc", ns):
        if loc_elem.text:
            urls.append(loc_elem.text.strip())

    # namespace がない、あるいは別の namespace である場合へのフォールバック
    if not urls:
        for loc_elem in root.findall(".//loc"):
            if loc_elem.text:
                urls.append(loc_elem.text.strip())

    # prefix が空でなければ、URL の path がその prefix で始まるものだけ残す
    filtered_urls = []
    for u in urls:
        if not prefix:
            filtered_urls.append(u)
        else:
            parsed = urllib.parse.urlparse(u)
            if parsed.path.startswith(prefix):
                filtered_urls.append(u)

    # ソートして安定順に
    filtered_urls.sort()
    return filtered_urls


class _RateLimiter:
    """全スレッド共通のレート制限。API 上限 600/分 を超えないようにする。"""

    def __init__(self, qps: float):
        self._min_interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


def inspect_urls(
    creds: tuple[str, str, str],
    site_url: str,
    urls: list[str],
    qps: float,
    workers: int,
    circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """各 URL を URL Inspection API で検査する。

    URL Inspection API は 1 URL あたり実測 6-7 秒かかる (2026-07-16 実測: 逐次で
    100 件 / 11 分)。1500 件超を逐次処理すると 3 時間近くかかり GitHub Actions の
    job timeout に載らないため、スレッドで並列化する。API 上限は 600/分なので
    数十並列でも余裕があるが、レートは _RateLimiter で全スレッド共通に絞る。

    #3372: circuit_breaker_threshold 件連続で 429/quotaExceeded を観測したら、
    以降の URL は API を叩かずに即座にエラー扱いで打ち切る (日次クォータ完全枯渇時の
    無駄なリトライ連打を防ぐ)。0 で無効化。
    """
    from googleapiclient.errors import HttpError

    total = len(urls)
    logger.info("starting inspection of %d urls (workers=%d, qps=%.1f, circuit_breaker_threshold=%d)",
                total, workers, qps, circuit_breaker_threshold)

    limiter = _RateLimiter(qps)
    # googleapiclient の service は thread-safe でないためスレッドごとに構築する
    local = threading.local()
    progress = {"done": 0}
    progress_lock = threading.Lock()

    circuit_open = threading.Event()
    quota_lock = threading.Lock()
    quota_state = {"consecutive_failures": 0}

    def _service():
        if not hasattr(local, "svc"):
            local.svc = _build_service(*creds)
        return local.svc

    def _circuit_enabled() -> bool:
        return circuit_breaker_threshold > 0

    def _trip_circuit() -> None:
        if not circuit_open.is_set():
            circuit_open.set()
            logger.error(
                "circuit breaker tripped: %d consecutive quota errors — aborting remaining "
                "URL requests without further retries (#3372)", circuit_breaker_threshold
            )

    def _one(url: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if _circuit_enabled() and circuit_open.is_set():
            return None, {"url": url, "error": "circuit_breaker_open: quota exhausted, request skipped"}

        max_retries = 3
        last_exception = None

        for attempt in range(max_retries + 1):
            if _circuit_enabled() and circuit_open.is_set():
                last_exception = last_exception or Exception("circuit breaker opened during retry wait")
                break
            try:
                limiter.acquire()
                res = _service().urlInspection().index().inspect(body={
                    "inspectionUrl": url,
                    "siteUrl": site_url,
                    "languageCode": "ja-JP",
                }).execute()
                inspection = res.get("inspectionResult", {})
                idx = inspection.get("indexStatusResult", {})
                # #5085: richResultsResult は同じレスポンスに既に入っており、
                # 拾っても **API 呼び出しは 1 件も増えない** (クォータ影響ゼロ)。
                # ここまで捨てていたため「Product / レビュースニペットが実際に
                # 有効か」を GSC UI を人が見に行く以外に確かめる手段が無かった。
                rich = _summarize_rich_results(inspection.get("richResultsResult", {}))
                if _circuit_enabled():
                    with quota_lock:
                        quota_state["consecutive_failures"] = 0
                return {
                    "url": url,
                    "verdict": idx.get("verdict"),
                    "coverage_state": idx.get("coverageState"),
                    "robots_txt_state": idx.get("robotsTxtState"),
                    "indexing_state": idx.get("indexingState"),
                    "page_fetch_state": idx.get("pageFetchState"),
                    "last_crawl_time": idx.get("lastCrawlTime"),
                    "google_canonical": idx.get("googleCanonical"),
                    "user_canonical": idx.get("userCanonical"),
                    "rich_verdict": rich["verdict"],
                    "rich_types": rich["types"],
                    "rich_issues": rich["issues"],
                }, None
            except HttpError as e:
                last_exception = e
                # クォータ超過は 429 だけでなく 403 (reason=quotaExceeded 等) でも返るため両方リトライ対象
                status = getattr(e.resp, "status", None)
                retryable = status == 429 or (status == 403 and b"uota" in (e.content or b""))
                if not retryable:
                    if _circuit_enabled():
                        with quota_lock:
                            quota_state["consecutive_failures"] = 0
                    break
                if _circuit_enabled():
                    with quota_lock:
                        quota_state["consecutive_failures"] += 1
                        should_trip = quota_state["consecutive_failures"] >= circuit_breaker_threshold
                    if should_trip:
                        _trip_circuit()
                    if circuit_open.is_set():
                        break
                if attempt < max_retries:
                    logger.warning("rate/quota limit (%s) at %s. sleeping 60s (attempt %d/%d)",
                                   status, url, attempt + 1, max_retries)
                    time.sleep(60)
                    continue
                break
            except Exception as e:
                last_exception = e
                if _circuit_enabled():
                    with quota_lock:
                        quota_state["consecutive_failures"] = 0
                break

        error_msg = str(last_exception)[:200] if last_exception else "circuit_breaker_open: quota exhausted, request skipped"
        logger.error("failed to inspect URL %s: %s", url, error_msg)
        return None, {"url": url, "error": error_msg}

    def _tracked(url: str):
        out = _one(url)
        with progress_lock:
            progress["done"] += 1
            done = progress["done"]
        if done % 100 == 0 or done == total:
            logger.info("progress: %d/%d URLs processed", done, total)
        return out

    inspected_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_tracked, u) for u in urls]
        for f in as_completed(futures):
            ok, err = f.result()
            if ok is not None:
                inspected_results.append(ok)
            else:
                errors.append(err)

    # 並列完了順に積まれるので、出力の安定のため URL 順に戻す
    inspected_results.sort(key=lambda r: r["url"])
    errors.sort(key=lambda r: r["url"])
    circuit_info = {
        "tripped": circuit_open.is_set(),
        "threshold": circuit_breaker_threshold,
    }
    return inspected_results, errors, circuit_info


def _summarize_rich_results(rich: dict[str, Any] | None) -> dict[str, Any]:
    """URL Inspection の ``richResultsResult`` を集計しやすい形に潰す (#5085)。

    API の形:

        {"verdict": "PASS",
         "detectedItems": [{"richResultType": "Product snippets",
                            "items": [{"name": "...",
                                       "issues": [{"issueMessage": "...",
                                                   "severity": "WARNING"}]}]}]}

    URL 1 本ぶんの生データをそのまま持つと census が肥大するので、
    ``verdict`` / 検出された rich result 種別 / 課題メッセージ (severity 付き)
    の 3 つだけ残す。**課題が 1 件も無いことと、そもそも検出されていないことを
    区別できるようにする**のが要点で、``types`` が空なら後者。

    リッチリザルトが無効化されていても GSC は 404 を返さず単に
    ``richResultsResult`` を欠くため、``verdict`` は "(none)" になる。
    """
    if not isinstance(rich, dict) or not rich:
        return {"verdict": "(none)", "types": [], "issues": []}
    types: list[str] = []
    issues: list[str] = []
    for det in rich.get("detectedItems") or []:
        if not isinstance(det, dict):
            continue
        rtype = det.get("richResultType") or "(unnamed)"
        types.append(rtype)
        for item in det.get("items") or []:
            if not isinstance(item, dict):
                continue
            for iss in item.get("issues") or []:
                if not isinstance(iss, dict):
                    continue
                sev = iss.get("severity") or "(none)"
                msg = iss.get("issueMessage") or "(no message)"
                issues.append(f"{rtype} / {sev}: {msg}")
    return {
        "verdict": rich.get("verdict") or "(none)",
        "types": sorted(set(types)),
        "issues": sorted(set(issues)),
    }


def build_not_indexed_urls(
    inspected: list[dict[str, Any]], max_items: int = DEFAULT_MAX_NOT_INDEXED_URLS
) -> list[dict[str, Any]]:
    """verdict != "PASS" の URL を抽出する。max_items <= 0 なら無制限。"""
    def _val(item: dict[str, Any], key: str) -> Any:
        v = item.get(key)
        return "(none)" if v is None else v

    rows: list[dict[str, Any]] = []
    for item in inspected:
        if item.get("verdict") == "PASS":
            continue
        rows.append({
            "url": item["url"],
            "coverage_state": _val(item, "coverage_state"),
            "verdict": _val(item, "verdict"),
            "last_crawl_time": _val(item, "last_crawl_time"),
            "google_canonical": _val(item, "google_canonical"),
        })
        if 0 < max_items <= len(rows):
            break
    return rows


def last_quota_reset_boundary(now: datetime, reset_hour_utc: int = DEFAULT_QUOTA_RESET_HOUR_UTC) -> datetime:
    """`now` 以前で直近のクォータリセット時刻 (UTC) を返す。"""
    boundary = now.replace(hour=reset_hour_utc, minute=0, second=0, microsecond=0)
    if boundary > now:
        boundary -= timedelta(days=1)
    return boundary


def check_quota_window(
    out_path: pathlib.Path, reset_hour_utc: int, now: datetime
) -> tuple[bool, str]:
    """#3372 pre-flight ガード: 前回成功 run からクォータリセットウィンドウを跨いだかを判定する。

    前回 run の記録が読めない場合は「跨いだ」扱いにして通す (壊さない側に倒す)。
    """
    if not out_path.exists():
        return True, "no prior census file found — nothing to guard against"

    try:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        last_fetched_at = datetime.fromisoformat(prev["fetched_at"])
    except Exception as e:
        return True, f"could not read/parse prior census file ({e}) — proceeding"

    boundary = last_quota_reset_boundary(now, reset_hour_utc)
    if last_fetched_at < boundary:
        return True, (
            f"quota window crossed (last run {last_fetched_at.isoformat()}, "
            f"reset boundary {boundary.isoformat()})"
        )

    next_boundary = boundary + timedelta(days=1)
    return False, (
        f"quota window NOT crossed since last successful run ({last_fetched_at.isoformat()}). "
        f"Google's URL Inspection API daily quota resets around Pacific midnight "
        f"(≈ UTC {reset_hour_utc:02d}:00), next boundary ≈ {next_boundary.isoformat()}. "
        f"Re-running now will likely hit 429 immediately. Pass --force (or workflow input "
        f"force=true) to override."
    )


def main() -> int:
    # Windows cp932 での文字化け防止のため標準出力を UTF-8 に変更
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--site-url", default=os.environ.get("GSC_SITE_URL"))
    p.add_argument("--client-id", default=os.environ.get("GSC_OAUTH_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("GSC_OAUTH_CLIENT_SECRET"))
    p.add_argument("--refresh-token", default=os.environ.get("GSC_OAUTH_REFRESH_TOKEN"))
    p.add_argument("--sitemap", default=DEFAULT_SITEMAP)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--qps", type=float, default=DEFAULT_QPS)
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help="並列数。URL Inspection API は 1 URL 6-7 秒かかるため逐次では現実的でない")
    p.add_argument("--max-not-indexed-urls", type=int, default=DEFAULT_MAX_NOT_INDEXED_URLS,
                   help="not_indexed_urls に保持する最大件数。0 (既定) で無制限")
    p.add_argument("--circuit-breaker-threshold", type=int, default=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
                   help="連続クォータエラー N 件で残り URL のリトライを打ち切る (#3372)。0 で無効化")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--preflight-check", action="store_true",
                   help="#3372: API を叩かず、前回成功 run からクォータリセットウィンドウを"
                        "跨いだかだけを判定して終了する (0=OK / 1=ブロック)")
    p.add_argument("--force", action="store_true",
                   help="--preflight-check のブロックを無視して常に成功扱いにする")
    p.add_argument("--quota-reset-hour-utc", type=int, default=DEFAULT_QUOTA_RESET_HOUR_UTC,
                   help="クォータリセット時刻 (UTC 時)。既定は Pacific 深夜の近似値 07:00")
    args = p.parse_args()

    if args.preflight_check:
        if args.force:
            logger.info("preflight check skipped: --force specified")
            return 0
        ok, msg = check_quota_window(pathlib.Path(args.out), args.quota_reset_hour_utc, datetime.now(timezone.utc))
        if ok:
            logger.info("preflight check passed: %s", msg)
            return 0
        logger.error("preflight check BLOCKED: %s", msg)
        return 1

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
        # Sitemap URL 取得
        sitemap_urls = fetch_sitemap_urls(args.sitemap, args.prefix)
        sitemap_count = len(sitemap_urls)

        # limit 適用
        if args.limit > 0:
            target_urls = sitemap_urls[:args.limit]
        else:
            target_urls = sitemap_urls

        creds = (args.client_id, args.client_secret, args.refresh_token)
        inspected, errors, circuit_info = inspect_urls(
            creds, args.site_url, target_urls, args.qps, args.workers,
            args.circuit_breaker_threshold,
        )

        # 集計処理
        total_sitemap_urls = sitemap_count
        total_inspected = len(target_urls)
        total_errors = len(errors)

        # verdict == "PASS" の数
        total_indexed = sum(1 for item in inspected if item.get("verdict") == "PASS")
        total_not_indexed = total_inspected - total_errors - total_indexed

        # collections.Counter での集計。None の場合は "(none)" に変換
        def process_counter(key_name: str) -> dict[str, int]:
            vals = []
            for item in inspected:
                v = item.get(key_name)
                if v is None:
                    vals.append("(none)")
                else:
                    vals.append(v)
            counter = collections.Counter(vals)
            # 件数降順でソートした dict を返す
            return dict(sorted(counter.items(), key=lambda x: x[1], reverse=True))

        by_coverage_state = process_counter("coverage_state")
        by_verdict = process_counter("verdict")
        by_robots_txt_state = process_counter("robots_txt_state")
        by_indexing_state = process_counter("indexing_state")

        # #5085: リッチリザルトの現況。by_rich_verdict は URL 単位 (1 URL 1 票)、
        # by_rich_type / rich_issues は 1 URL が複数種別・複数課題を持つので
        # 延べ数になる。分母が違うので合計は一致しない。
        by_rich_verdict = process_counter("rich_verdict")
        rich_type_counter: collections.Counter = collections.Counter()
        rich_issue_counter: collections.Counter = collections.Counter()
        for item in inspected:
            rich_type_counter.update(item.get("rich_types") or [])
            rich_issue_counter.update(item.get("rich_issues") or [])
        by_rich_type = dict(sorted(rich_type_counter.items(),
                                   key=lambda x: (-x[1], x[0])))
        # 課題メッセージは種類が増えても全部残す。上限で切ると
        # 「1 種類だけ大量に出ている」のか「多品種が少しずつ」なのかが消える。
        rich_issues = dict(sorted(rich_issue_counter.items(),
                                  key=lambda x: (-x[1], x[0])))

        not_indexed_urls = build_not_indexed_urls(inspected, args.max_not_indexed_urls)

        result = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "site_url": args.site_url,
            "sitemap": args.sitemap,
            "prefix": args.prefix,
            "totals": {
                "sitemap_urls": total_sitemap_urls,
                "inspected": total_inspected,
                "errors": total_errors,
                "indexed": total_indexed,
                "not_indexed": total_not_indexed,
            },
            "by_coverage_state": by_coverage_state,
            "by_verdict": by_verdict,
            "by_robots_txt_state": by_robots_txt_state,
            "by_indexing_state": by_indexing_state,
            "by_rich_verdict": by_rich_verdict,
            "by_rich_type": by_rich_type,
            "rich_issues": rich_issues,
            "not_indexed_urls": not_indexed_urls,
            "errors": errors,
            "circuit_breaker": circuit_info,
        }

        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        if circuit_info["tripped"]:
            logger.warning(
                "wrote %s (PARTIAL — circuit breaker tripped: inspected=%d, indexed=%d, "
                "not_indexed=%d, errors=%d)",
                out, total_inspected, total_indexed, total_not_indexed, total_errors
            )
        else:
            logger.info(
                "wrote %s (inspected=%d, indexed=%d, not_indexed=%d, errors=%d)",
                out, total_inspected, total_indexed, total_not_indexed, total_errors
            )

    except Exception as e:
        logger.exception("GSC index inspection failed: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
