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
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inspect_gsc_index")

DEFAULT_OUT = "data/analytics/gsc_index_census.json"
DEFAULT_SITEMAP = "https://navi.omcha.jp/sitemap.xml"
DEFAULT_PREFIX = "/products/"
DEFAULT_LIMIT = 1800
DEFAULT_QPS = 5.0


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


def inspect_urls(
    service: Any,
    site_url: str,
    urls: list[str],
    qps: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """各 URL を URL Inspection API で検査する。"""
    from googleapiclient.errors import HttpError

    inspected_results = []
    errors = []
    total = len(urls)

    logger.info("starting inspection of %d urls with QPS %.1f", total, qps)

    interval = 1.0 / qps if qps > 0 else 0.0

    for i, url in enumerate(urls):
        # 各リクエストの間にスロットリングを挟む
        if i > 0 and interval > 0:
            time.sleep(interval)

        max_retries = 3
        last_exception = None
        res = None

        for attempt in range(max_retries + 1):
            try:
                res = service.urlInspection().index().inspect(body={
                    "inspectionUrl": url,
                    "siteUrl": site_url,
                    "languageCode": "ja-JP",
                }).execute()
                break  # 成功
            except HttpError as e:
                last_exception = e
                # クォータ超過は 429 だけでなく 403 (reason=quotaExceeded 等) でも返るため両方リトライ対象
                status = getattr(e.resp, "status", None)
                retryable = status == 429 or (status == 403 and b"uota" in (e.content or b""))
                if retryable and attempt < max_retries:
                    logger.warning(
                        "rate/quota limit (%s) hit at URL: %s. sleeping 60s (attempt %d/%d)",
                        status, url, attempt + 1, max_retries
                    )
                    time.sleep(60)
                    continue
                break  # リトライ不能、あるいはリトライ上限超過
            except Exception as e:
                last_exception = e
                break  # その他のエラーは即時失敗

        if res is not None:
            r = res.get("inspectionResult", {})
            idx = r.get("indexStatusResult", {})

            result_item = {
                "url": url,
                "verdict": idx.get("verdict"),
                "coverage_state": idx.get("coverageState"),
                "robots_txt_state": idx.get("robotsTxtState"),
                "indexing_state": idx.get("indexingState"),
                "page_fetch_state": idx.get("pageFetchState"),
                "last_crawl_time": idx.get("lastCrawlTime"),
                "google_canonical": idx.get("googleCanonical"),
                "user_canonical": idx.get("userCanonical"),
            }
            inspected_results.append(result_item)
        else:
            error_msg = str(last_exception)[:200]
            logger.error("failed to inspect URL %s: %s", url, error_msg)
            errors.append({
                "url": url,
                "error": error_msg
            })

        # 100 件ごとに進捗を出力
        if (i + 1) % 100 == 0 or (i + 1) == total:
            logger.info("progress: %d/%d URLs processed", i + 1, total)

    return inspected_results, errors


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
        # Sitemap URL 取得
        sitemap_urls = fetch_sitemap_urls(args.sitemap, args.prefix)
        sitemap_count = len(sitemap_urls)

        # limit 適用
        if args.limit > 0:
            target_urls = sitemap_urls[:args.limit]
        else:
            target_urls = sitemap_urls

        service = _build_service(args.client_id, args.client_secret, args.refresh_token)

        inspected, errors = inspect_urls(service, args.site_url, target_urls, args.qps)

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

        # verdict != "PASS" のものを最大 300 件まで
        not_indexed_urls = []
        for item in inspected:
            if item.get("verdict") != "PASS":
                not_indexed_urls.append({
                    "url": item["url"],
                    "coverage_state": item["coverage_state"] if item["coverage_state"] is not None else "(none)",
                    "verdict": item["verdict"] if item["verdict"] is not None else "(none)",
                    "last_crawl_time": item["last_crawl_time"] if item["last_crawl_time"] is not None else "(none)",
                    "google_canonical": item["google_canonical"] if item["google_canonical"] is not None else "(none)",
                })
                if len(not_indexed_urls) >= 300:
                    break

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
            "not_indexed_urls": not_indexed_urls,
            "errors": errors,
        }

        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

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
