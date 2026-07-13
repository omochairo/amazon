"""audit_site_health.py

navi.omcha.jp の自サイト定期クロールによるインデックス健全性監査 (#2995 案2)。
自宅 NAS の self-hosted runner から週次で実行する想定。report-only であり、
記事生成パイプラインには一切副作用を与えない (読み取り専用クロール + JSON 出力のみ)。

背景・目的:
  sitemap.xml と実際のクロール結果を突き合わせ、以下の regression を検知する。
  特に #2701 (謎の noindex 混入) の定期監視を主目的とする。
    R1 sitemap_broken       : sitemap 掲載 URL が 200 以外 / リダイレクトしている
    R2 sitemap_noindex      : sitemap 掲載 URL が noindex になっている (#2701 本体)
    R3 broken_internal      : 内部リンクされている 404/410 (リンク切れ)
    R4 server_errors        : 5xx またはフェッチ失敗 (status 0)
    R5 orphans              : sitemap 掲載だが内部リンクの参照が 0 件 (孤立ページ)
    R6 info                 : canonical 不一致件数 / noindex なのに内部リンクされている件数
  R1-R4 の合計が 1 件以上あれば has_regressions=True。

クロール設計:
  Phase 1 (sitemap): {base}/sitemap.xml を取得。sitemapindex ならず子 sitemap も
    再帰的に取得し、urlset の <loc> 群を「監視対象の sitemap URL 集合」とする。
  Phase 2 (BFS): base-url のトップページから <a href> を辿る素朴な BFS クロール。
    HTML (Content-Type: text/html) のみ本文パースし、noindex / canonical /
    JSON-LD 件数 / 内部リンク先を記録する。--max-pages / --limit で件数上限。
  Phase 3 (stragglers): sitemap 掲載 URL のうち BFS で未到達だったものも追加取得
    する (取得はするが、そこからのリンクはさらに辿らない)。BFS 到達していない
    = 内部リンクから辿れない = 孤立候補という前提のため、被リンク件数は 0 として
    扱う (真の被リンク元がクロール範囲外にあっても検知対象を単純化するための
    仕様上の割り切り)。

HTTP は全て fetch_url() 経由 (session を渡すだけの module-level 関数) にして
テストから差し替え可能にしている。シングルスレッド、リクエスト毎に
time.sleep(--delay) する。個別 URL の例外は status=0 として記録し継続する
(#2953 の fetch_google_suggest のような「中断」ではなく、監査対象が多いため
1 URL の失敗で全体を止めない設計)。

出力:
  --out-dir/latest.json  : 直近実行の regression 系一覧のみ (全クロールページの
    ダンプはしない。diff を小さく保つため)。
  --out-dir/history.jsonl: 1 行 = 1 回の実行サマリを追記。

呼び出し:
  `python scripts/audit_site_health.py` / `python -m scripts.audit_site_health`
  いずれでも実行可能 (相対 import なし)。

Issue: https://github.com/omochairo/amazon/issues/2995 (案2)
Issue: https://github.com/omochairo/amazon/issues/2701 (sitemap noindex regression watch)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_site_health")

DEFAULT_BASE_URL = "https://navi.omcha.jp"
DEFAULT_MAX_PAGES = 20000
DEFAULT_DELAY = 0.2
DEFAULT_TIMEOUT = 15
DEFAULT_OUT_DIR = "data/site_audit"
DEFAULT_USER_AGENT = "navi-site-audit/1.0 (+https://navi.omcha.jp)"
LOG_EVERY = 200

# 内部リンク / sitemap urlset <loc> の正規化時に除外する典型的なアセット拡張子。
ASSET_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".ico",
    ".xml", ".txt", ".pdf", ".json", ".woff", ".woff2",
)


@dataclass
class FetchResult:
    """1 リクエスト分の生の結果。status=0 は例外発生 (接続失敗等) を意味する。"""

    status: int
    final_url: str
    content_type: str = ""
    text: str = ""
    headers: dict = field(default_factory=dict)


@dataclass
class PageRecord:
    """1 URL をクロールした結果の正規化済みレコード (inbound は後で全体計算)。"""

    status: int
    final_url: str
    redirected: bool
    noindex: bool
    canonical: str | None
    jsonld_count: int
    outlinks: set[str] = field(default_factory=set)


def fetch_url(session: requests.Session, url: str, timeout: float, user_agent: str) -> FetchResult:
    """単一 URL を GET する (差し替え可能な唯一の HTTP 入口)。

    リクエスト例外は捕捉して status=0 の FetchResult を返す (呼び出し側は継続する)。
    """
    try:
        resp = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": user_agent},
        )
    except requests.RequestException as e:
        logger.warning("fetch failed: %s (%s)", url, e)
        return FetchResult(status=0, final_url=url)
    return FetchResult(
        status=resp.status_code,
        final_url=resp.url,
        content_type=resp.headers.get("Content-Type", "") or "",
        text=resp.text or "",
        headers=dict(resp.headers),
    )


def _header_value(headers: dict, name: str) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v or ""
    return ""


def normalize_url(url: str, base_url: str, host: str) -> str | None:
    """内部リンク / sitemap urlset <loc> 用の正規化 (純粋関数)。

    base_url に対する相対解決、fragment/query の除去、base-url と同一ホストのみ許可
    (http(s) のみ)、拡張子なしパスは末尾スラッシュに統一、既知アセット拡張子は除外する。
    対象外の場合は None を返す。
    """
    if not url:
        return None
    absolute = urljoin(base_url, url)
    parsed = urlsplit(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() != host.lower():
        return None
    path = parsed.path or "/"
    if path.lower().endswith(ASSET_EXTENSIONS):
        return None
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment and not path.endswith("/"):
        path = path + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _resolve_sitemap_ref(url: str, base_url: str, host: str) -> str | None:
    """sitemapindex 内の子 sitemap <loc> 用の軽量正規化。

    normalize_url と違い、.xml を落とさず末尾スラッシュも付与しない (sitemap ファイル
    自体はアセット扱いせず、ファイル名をそのまま保持する必要があるため)。
    """
    if not url:
        return None
    absolute = urljoin(base_url, url)
    parsed = urlsplit(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() != host.lower():
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sitemap_root_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/sitemap.xml"


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_sitemap_xml(xml_text: str) -> tuple[str, list[str]]:
    """sitemap XML をパースし (種別, loc 一覧) を返す。種別は urlset/sitemapindex/unknown。"""
    if not xml_text:
        return "unknown", []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("sitemap XML parse error: %s", e)
        return "unknown", []
    kind = _local_tag(root.tag)
    locs: list[str] = []
    for child in root:
        for grandchild in child:
            if _local_tag(grandchild.tag) == "loc" and grandchild.text:
                locs.append(grandchild.text.strip())
    return kind, locs


def collect_sitemap_urls(
    base_url: str,
    host: str,
    session: requests.Session,
    timeout: float,
    user_agent: str,
    fetch_fn,
    sleeper,
    delay: float,
) -> set[str]:
    """Phase 1: sitemap.xml (+ sitemapindex 経由の子 sitemap) から監視対象 URL 集合を作る。"""
    to_fetch = [_sitemap_root_url(base_url)]
    visited_files: set[str] = set()
    content_urls: set[str] = set()
    while to_fetch:
        sm_url = to_fetch.pop(0)
        if sm_url in visited_files:
            continue
        visited_files.add(sm_url)
        result = fetch_fn(session, sm_url, timeout, user_agent)
        sleeper(delay)
        if result.status != 200:
            logger.warning("sitemap fetch failed: %s (status=%s)", sm_url, result.status)
            continue
        kind, locs = parse_sitemap_xml(result.text)
        if kind == "sitemapindex":
            for loc in locs:
                child = _resolve_sitemap_ref(loc, sm_url, host)
                if child and child not in visited_files:
                    to_fetch.append(child)
        elif kind == "urlset":
            for loc in locs:
                norm = normalize_url(loc, sm_url, host)
                if norm:
                    content_urls.add(norm)
        else:
            logger.warning("unrecognized sitemap root element at %s", sm_url)
    return content_urls


def _meta_robots_noindex(soup: BeautifulSoup) -> bool:
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        if name != "robots":
            continue
        content = (meta.get("content") or "").lower()
        if "noindex" in content:
            return True
    return False


def _extract_canonical(soup: BeautifulSoup, page_url: str, host: str) -> str | None:
    for link in soup.find_all("link"):
        rel = link.get("rel")
        rel_values = rel if isinstance(rel, list) else ([rel] if rel else [])
        if any((r or "").strip().lower() == "canonical" for r in rel_values):
            href = link.get("href")
            if href:
                return normalize_url(href, page_url, host)
    return None


def process_fetch(url: str, result: FetchResult, host: str) -> PageRecord:
    """FetchResult を PageRecord に変換する (noindex/canonical/jsonld/outlinks を抽出)。"""
    if result.status == 0:
        final_norm = url
    else:
        final_norm = normalize_url(result.final_url, result.final_url, host) or url
    redirected = result.status != 0 and final_norm != url

    noindex = "noindex" in _header_value(result.headers, "X-Robots-Tag").lower()
    canonical: str | None = None
    jsonld_count = 0
    outlinks: set[str] = set()

    is_html = "text/html" in (result.content_type or "").lower()
    if is_html and result.text:
        soup = BeautifulSoup(result.text, "html.parser")
        if _meta_robots_noindex(soup):
            noindex = True
        canonical = _extract_canonical(soup, url, host)
        jsonld_count = len(soup.find_all("script", attrs={"type": "application/ld+json"}))
        for a in soup.find_all("a", href=True):
            norm = normalize_url(a["href"], url, host)
            if norm:
                outlinks.add(norm)

    return PageRecord(
        status=result.status,
        final_url=final_norm,
        redirected=redirected,
        noindex=noindex,
        canonical=canonical,
        jsonld_count=jsonld_count,
        outlinks=outlinks,
    )


def crawl_site(
    base_url: str,
    host: str,
    session: requests.Session,
    max_pages: int,
    delay: float,
    timeout: float,
    user_agent: str,
    effective_limit: int | None,
    fetch_fn,
    sleeper,
) -> dict[str, PageRecord]:
    """Phase 2: base-url のホームページから <a href> を辿る BFS クロール。"""
    cap = max_pages if not effective_limit else min(max_pages, effective_limit)
    start = normalize_url(base_url, base_url, host) or (base_url.rstrip("/") + "/")

    queue: deque[str] = deque([start])
    seen = {start}
    records: dict[str, PageRecord] = {}
    fetched = 0

    while queue and fetched < cap:
        url = queue.popleft()
        result = fetch_fn(session, url, timeout, user_agent)
        record = process_fetch(url, result, host)
        records[url] = record
        fetched += 1
        if fetched % LOG_EVERY == 0:
            logger.info("crawled %d pages so far...", fetched)

        for link in sorted(record.outlinks):
            if link not in seen and len(seen) < cap:
                seen.add(link)
                queue.append(link)

        sleeper(delay)

    return records


def fetch_stragglers(
    sitemap_urls: set[str],
    records: dict[str, PageRecord],
    host: str,
    session: requests.Session,
    timeout: float,
    user_agent: str,
    fetch_fn,
    sleeper,
    delay: float,
) -> set[str]:
    """Phase 3: sitemap 掲載だが BFS で未到達だった URL を追加取得する。取得した URL 集合を返す。"""
    stragglers = sorted(u for u in sitemap_urls if u not in records)
    for url in stragglers:
        result = fetch_fn(session, url, timeout, user_agent)
        records[url] = process_fetch(url, result, host)
        sleeper(delay)
    return set(stragglers)


def compute_inbound(records: dict[str, PageRecord], straggler_urls: set[str]) -> dict[str, set[str]]:
    """クロール済みページ間の内部リンクグラフから被リンク元集合を計算する (自己リンク除外)。

    straggler (BFS 未到達) は仕様上、被リンク 0 件として扱う。
    """
    inbound: dict[str, set[str]] = defaultdict(set)
    for src, rec in records.items():
        for target in rec.outlinks:
            if target == src:
                continue
            inbound[target].add(src)
    for url in straggler_urls:
        inbound[url] = set()
    return inbound


def run(
    base_url: str = DEFAULT_BASE_URL,
    max_pages: int = DEFAULT_MAX_PAGES,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    out_dir: pathlib.Path | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    limit: int = 0,
    session: requests.Session | None = None,
    fetch_fn=fetch_url,
    sleeper=time.sleep,
    now: datetime | None = None,
) -> dict:
    """監査を実行し、latest.json / history.jsonl を書き出して結果 dict を返す。"""
    out_dir = out_dir if out_dir is not None else pathlib.Path(DEFAULT_OUT_DIR)
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)

    parsed_base = urlsplit(base_url)
    host = parsed_base.netloc

    sitemap_urls = collect_sitemap_urls(base_url, host, session, timeout, user_agent, fetch_fn, sleeper, delay)
    logger.info("sitemap urls collected: %d", len(sitemap_urls))

    effective_limit = limit if limit and limit > 0 else None
    records = crawl_site(base_url, host, session, max_pages, delay, timeout, user_agent, effective_limit, fetch_fn, sleeper)
    logger.info("BFS crawl done: %d pages", len(records))

    straggler_urls = fetch_stragglers(sitemap_urls, records, host, session, timeout, user_agent, fetch_fn, sleeper, delay)
    if straggler_urls:
        logger.info("sitemap stragglers fetched: %d", len(straggler_urls))

    inbound = compute_inbound(records, straggler_urls)

    homepage = normalize_url(base_url, base_url, host) or (base_url.rstrip("/") + "/")

    r1_sitemap_broken = []
    for u in sorted(sitemap_urls):
        rec = records.get(u)
        if rec is None:
            continue
        if rec.status != 200 or rec.redirected:
            r1_sitemap_broken.append({"url": u, "status": rec.status, "final_url": rec.final_url})

    r2_sitemap_noindex = sorted(u for u in sitemap_urls if records.get(u) and records[u].noindex)

    r3_broken_internal = []
    for u, rec in records.items():
        if rec.status in (404, 410) and len(inbound.get(u, ())) >= 1:
            sources = sorted(inbound[u])[:5]
            r3_broken_internal.append({"url": u, "status": rec.status, "sources": sources})
    r3_broken_internal.sort(key=lambda x: x["url"])

    r4_server_errors = []
    for u, rec in records.items():
        if rec.status >= 500 or rec.status == 0:
            r4_server_errors.append({"url": u, "status": rec.status})
    r4_server_errors.sort(key=lambda x: x["url"])

    r5_orphans = sorted(u for u in sitemap_urls if u != homepage and len(inbound.get(u, ())) == 0)

    canonical_mismatch_count = sum(
        1 for rec in records.values() if rec.canonical and rec.canonical != rec.final_url
    )
    noindex_inlinked_count = sum(
        1 for u, rec in records.items() if rec.noindex and len(inbound.get(u, ())) > 0
    )

    has_regressions = (
        len(r1_sitemap_broken) + len(r2_sitemap_noindex) + len(r3_broken_internal) + len(r4_server_errors)
    ) > 0

    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base_url,
        "pages_crawled": len(records),
        "sitemap_urls": len(sitemap_urls),
        "has_regressions": has_regressions,
        "summary": {
            "r1_sitemap_broken": len(r1_sitemap_broken),
            "r2_sitemap_noindex": len(r2_sitemap_noindex),
            "r3_broken_internal": len(r3_broken_internal),
            "r4_server_errors": len(r4_server_errors),
            "r5_orphans": len(r5_orphans),
            "canonical_mismatch_count": canonical_mismatch_count,
            "noindex_inlinked_count": noindex_inlinked_count,
        },
        "r1_sitemap_broken": r1_sitemap_broken,
        "r2_sitemap_noindex": r2_sitemap_noindex,
        "r3_broken_internal": r3_broken_internal,
        "r4_server_errors": r4_server_errors,
        "r5_orphans": r5_orphans,
    }

    write_outputs(out_dir, result, now)
    print_summary(result)
    return result


def write_outputs(out_dir: pathlib.Path, result: dict, now: datetime) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    latest_path = out_dir / "latest.json"
    latest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )

    history_entry = {
        "date": now.strftime("%Y-%m-%d"),
        "pages_crawled": result["pages_crawled"],
        "sitemap_urls": result["sitemap_urls"],
        "r1": result["summary"]["r1_sitemap_broken"],
        "r2": result["summary"]["r2_sitemap_noindex"],
        "r3": result["summary"]["r3_broken_internal"],
        "r4": result["summary"]["r4_server_errors"],
        "orphans": result["summary"]["r5_orphans"],
        "has_regressions": result["has_regressions"],
    }
    history_path = out_dir / "history.jsonl"
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")


def print_summary(result: dict) -> None:
    s = result["summary"]
    logger.info("---- site health audit summary ----")
    print(f"navi.omcha.jp site health audit - {result['generated_at']}")
    print(f"base_url={result['base_url']} pages_crawled={result['pages_crawled']} sitemap_urls={result['sitemap_urls']}")
    print(
        f"R1 sitemap_broken={s['r1_sitemap_broken']} R2 sitemap_noindex={s['r2_sitemap_noindex']} "
        f"R3 broken_internal={s['r3_broken_internal']} R4 server_errors={s['r4_server_errors']}"
    )
    print(
        f"R5 orphans={s['r5_orphans']} canonical_mismatch={s['canonical_mismatch_count']} "
        f"noindex_inlinked={s['noindex_inlinked_count']}"
    )
    if result["has_regressions"]:
        print("REGRESSIONS DETECTED")
    else:
        print("no regressions detected")


def main() -> None:
    ap = argparse.ArgumentParser(description="navi.omcha.jp 自サイトクロールによるインデックス健全性監査 (#2995 案2)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="監査対象のベース URL")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="BFS クロールの最大ページ数")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="リクエスト間 sleep (秒)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="リクエストタイムアウト (秒)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="出力先ディレクトリ")
    ap.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="リクエスト User-Agent")
    ap.add_argument("--limit", type=int, default=0, help="0=無制限。>0 でスモークテスト用にクロール件数を制限")
    ap.add_argument("--fail-on-regression", action="store_true", help="regression 検知時に exit code 2 を返す (デフォルトでは常に 0)")
    args = ap.parse_args()

    result = run(
        base_url=args.base_url,
        max_pages=args.max_pages,
        delay=args.delay,
        timeout=args.timeout,
        out_dir=pathlib.Path(args.out_dir),
        user_agent=args.user_agent,
        limit=args.limit,
    )

    if args.fail_on_regression and result["has_regressions"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
