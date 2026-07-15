"""crawl_yahoo_reviews.py

Issue #3203 Phase 2 Lane 2: Yahoo!ショッピングのレビューを未ログイン・低速蓄積で
収集し、`EXPERIENCE_RAW_DIR` (既定 `~/.omochairo/yahoo_reviews_raw`) にローカル保存
専用で書き出す。**リポジトリ内パスには絶対に書かない** (原文はサイト非公開・
gemma で言い換えた集合傾向のみを experience.json 経由でリポジトリに出す設計、
docs/article-quality-overhaul-design.md §5.2/§5.3)。

設計条件 (すべて必須):
  1. 未ログイン・自宅回線 (K8/NAS レーン)・夜間スケジュール
  2. 1 リクエスト 15〜30 秒間隔 + ジッター、robots.txt 尊重、正直な UA
     (連絡先 URL 入り)。bot 検知の回避策は実装しない
  3. 対象は監査対象/生成予定 ASIN に限定 (mine_experience.select_targets を流用)、
     日次上限あり (--max-requests, 既定 60)
  4. 原文はランナーのローカル保管のみ。既取得 ASIN は --refresh-days (既定 30) 以内
     なら skip (冪等・蓄積型)

URL 導出方式:
  data/raw/yahoo_matched.json の matched_asin 一致エントリの `url` は
  ValueCommerce のアフィリエイトリンク (`https://ck.jp.ap.valuecommerce.com/
  servlet/referral?sid=...&pid=...&vc_url=<url-encoded 実 URL>`)。
  `vc_url` パラメータをデコードすると実商品ページ
  `https://store.shopping.yahoo.co.jp/<store>/<item_code>.html` が得られる
  (scripts/fetch_yahoo.py の VC_REFERRAL_BASE と同じ仕組み)。
  Yahoo!ショッピングの慣例では、この商品ページの `<item_code>.html` を
  `review/<item_code>.html` に置き換えるとレビュー一覧ページになる。ページングは
  `?p=<page>` クエリ (1 ASIN あたり最大 3 ページ)。この慣例に一致しない URL は
  導出不能として ASIN を skip する。

レビュー抽出: Yahoo!ショッピングは検索エンジン向けリッチスニペット用に
schema.org Review/AggregateRating を JSON-LD (`<script type="application/ld+json">`)
で埋め込むことが多い。CSS セレクタは markup 変更に弱いため、比較的安定した
JSON-LD を抽出戦略として採用する。見つからなければそのページのレビュー 0 件として
扱う (クラッシュしない・次ページ探索も打ち切る)。

Usage:
    python scripts/crawl_yahoo_reviews.py --limit 20
    python scripts/crawl_yahoo_reviews.py --asins B0XXXXXXXX --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mine_experience import select_targets  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crawl_yahoo_reviews")

DEFAULT_MATCHED_PATH = pathlib.Path("data/raw/yahoo_matched.json")
DEFAULT_RAW_DIR = pathlib.Path.home() / ".omochairo" / "yahoo_reviews_raw"
DEFAULT_MAX_REQUESTS = 60
DEFAULT_REFRESH_DAYS = 30
MAX_PAGES_PER_ASIN = 3
REQUEST_TIMEOUT = 20

# 連絡先 URL 入りの正直な UA (bot 検知回避策は実装しない設計要件)。
HONEST_UA = "omochairo-experience-bot/1.0 (+https://navi.omcha.jp/)"

_PRODUCT_URL_RE = re.compile(
    r"^https://store\.shopping\.yahoo\.co\.jp/([^/]+)/([^/?#]+?)(?:\.html)?/?(?:[?#].*)?$"
)

_BOT_WALL_MARKERS = (
    "captcha", "recaptcha", "are you a human", "unusual traffic",
    "アクセスが集中", "自動的なアクセス", "自動アクセス",
)


class BotWallDetected(Exception):
    """bot 検知/CAPTCHA に遭遇した。回避策は実装せず、その ASIN を諦める。"""


class RequestBudget:
    """日次上限 (--max-requests) を横断的に消費する簡易カウンタ。"""

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.consumed = 0

    def exhausted(self) -> bool:
        return self.max_requests > 0 and self.consumed >= self.max_requests

    def try_consume(self) -> bool:
        if self.exhausted():
            return False
        self.consumed += 1
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def default_raw_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("EXPERIENCE_RAW_DIR", str(DEFAULT_RAW_DIR)))


# --------------------------------------------------------------------------
# URL 導出
# --------------------------------------------------------------------------

def resolve_product_url(raw_url: str | None) -> str | None:
    """yahoo_matched.json の `url` (ValueCommerce referral) から実商品ページ URL を
    取り出す。既に store.shopping.yahoo.co.jp の直リンクならそのまま返す。"""
    if not isinstance(raw_url, str) or not raw_url:
        return None
    parsed = urllib.parse.urlparse(raw_url)
    if "valuecommerce.com" in parsed.netloc:
        qs = urllib.parse.parse_qs(parsed.query)
        vc = qs.get("vc_url")
        if vc and vc[0]:
            return vc[0]
        return None
    return raw_url


def select_yahoo_url(asin: str, matched_path: pathlib.Path = DEFAULT_MATCHED_PATH) -> str | None:
    matched = _load(matched_path)
    items = matched.get("items", []) if isinstance(matched, dict) else []
    for it in items:
        if isinstance(it, dict) and it.get("matched_asin") == asin:
            resolved = resolve_product_url(it.get("url"))
            if resolved:
                return resolved
    return None


def derive_review_urls(product_url: str, max_pages: int = MAX_PAGES_PER_ASIN) -> list[str]:
    """商品ページ URL → レビューページ URL 群 (最大 max_pages 件) を導出する。
    店舗慣例に一致しない URL は導出不能として空リストを返す。"""
    m = _PRODUCT_URL_RE.match(product_url or "")
    if not m:
        return []
    store, item = m.group(1), m.group(2)
    base = f"https://store.shopping.yahoo.co.jp/{store}/review/{item}.html"
    urls = [base]
    for p in range(2, max_pages + 1):
        urls.append(f"{base}?p={p}")
    return urls


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

def robots_allowed(url: str, ua: str = HONEST_UA) -> bool:
    """robots.txt を確認する。取得自体に失敗した場合は安全側 (disallow) に倒す。"""
    try:
        parsed = urllib.parse.urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(ua, url)
    except Exception as e:  # noqa: BLE001 — robots.txt 取得失敗は安全側 (disallow)
        logger.warning("robots.txt check failed for %s: %s — treating as disallow", url, e)
        return False


# --------------------------------------------------------------------------
# fetch (レート制御・リトライ 1 回まで・bot wall 検知)
# --------------------------------------------------------------------------

def _looks_like_bot_wall(status_code: int, text: str) -> bool:
    if status_code in (403, 429):
        return True
    low = (text or "")[:4000].lower()
    return any(marker in low for marker in _BOT_WALL_MARKERS)


def fetch_with_retry(
    url: str, session: requests.Session, budget: RequestBudget,
    sleeper=time.sleep, rng: random.Random | Any = random,
) -> str | None:
    """1 URL を取得する。リクエスト前に必ず 15〜30 秒 + ジッター sleep する。
    リトライは 1 回まで。bot wall 検知時は BotWallDetected を送出する
    (呼び出し側でその ASIN を諦めて次へ進む)。"""
    for attempt in (1, 2):
        if not budget.try_consume():
            logger.info("request budget exhausted — stopping")
            return None
        sleeper(rng.uniform(15, 30))
        try:
            resp = session.get(url, headers={"User-Agent": HONEST_UA}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == 2:
                logger.warning("fetch failed for %s: %s", url, e)
                return None
            continue
        if _looks_like_bot_wall(resp.status_code, resp.text):
            raise BotWallDetected(url)
        if resp.status_code == 404:
            return None
        if resp.status_code == 200:
            return resp.text
        if attempt == 2:
            logger.warning("fetch failed for %s: HTTP %s", url, resp.status_code)
            return None
    return None


# --------------------------------------------------------------------------
# レビュー抽出 (schema.org JSON-LD)
# --------------------------------------------------------------------------

def _parse_review_node(node: dict) -> dict | None:
    body = node.get("reviewBody")
    if not isinstance(body, str) or not body.strip():
        return None
    rating = None
    rr = node.get("reviewRating")
    if isinstance(rr, dict):
        try:
            rating = float(rr.get("ratingValue"))
        except (TypeError, ValueError):
            rating = None
    title = node.get("name") if isinstance(node.get("name"), str) else ""
    posted_at = node.get("datePublished") if isinstance(node.get("datePublished"), str) else ""
    return {"rating": rating, "title": title, "body": body.strip(), "posted_at": posted_at}


def _reviews_from_ldjson_node(node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for n in node:
            out.extend(_reviews_from_ldjson_node(n))
        return out
    if not isinstance(node, dict):
        return out
    graph = node.get("@graph")
    if isinstance(graph, list):
        out.extend(_reviews_from_ldjson_node(graph))
    node_type = node.get("@type")
    if node_type == "Review" or (isinstance(node_type, list) and "Review" in node_type):
        r = _parse_review_node(node)
        if r:
            out.append(r)
    review_field = node.get("review")
    if isinstance(review_field, (list, dict)):
        out.extend(_reviews_from_ldjson_node(review_field))
    return out


def extract_reviews(html: str) -> list[dict]:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        blocks = [s.string or s.get_text() for s in soup.find_all("script", attrs={"type": "application/ld+json"})]
    except ImportError:
        blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, flags=re.S | re.I,
        )

    reviews: list[dict] = []
    for block in blocks:
        if not block or not block.strip():
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        reviews.extend(_reviews_from_ldjson_node(data))
    return reviews


# --------------------------------------------------------------------------
# 冪等・蓄積 (refresh-days)
# --------------------------------------------------------------------------

def is_fresh(path: pathlib.Path, max_age_days: int) -> bool:
    data = _load(path)
    if not isinstance(data, dict):
        return False
    ts = data.get("fetched_at")
    if not isinstance(ts, str):
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return (now - when).days < max_age_days


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def crawl_asin(
    asin: str, *,
    matched_path: pathlib.Path = DEFAULT_MATCHED_PATH,
    session: requests.Session, budget: RequestBudget,
    sleeper=time.sleep, rng: random.Random | Any = random,
    max_pages: int = MAX_PAGES_PER_ASIN,
) -> dict | None:
    """1 ASIN 分を収集する。取得不能・bot wall・レビュー 0 件は None を返す。"""
    product_url = select_yahoo_url(asin, matched_path)
    if not product_url:
        logger.info("%s: no matched Yahoo URL — skip", asin)
        return None
    review_urls = derive_review_urls(product_url, max_pages=max_pages)
    if not review_urls:
        logger.info("%s: could not derive review URL from %s — skip", asin, product_url)
        return None
    if not robots_allowed(review_urls[0]):
        logger.info("%s: robots.txt disallows — skip", asin)
        return None

    all_reviews: list[dict] = []
    for url in review_urls:
        if budget.exhausted():
            break
        try:
            html = fetch_with_retry(url, session, budget, sleeper=sleeper, rng=rng)
        except BotWallDetected:
            logger.warning("%s: bot wall detected at %s — abandoning ASIN (no workaround)", asin, url)
            return None
        if html is None:
            break
        page_reviews = extract_reviews(html)
        if not page_reviews:
            break
        all_reviews.extend(page_reviews)

    if not all_reviews:
        return None
    return {"asin": asin, "fetched_at": _now_iso(), "reviews": all_reviews}


def run(
    targets: list[str], *,
    matched_path: pathlib.Path = DEFAULT_MATCHED_PATH,
    raw_dir: pathlib.Path | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
    max_pages: int = MAX_PAGES_PER_ASIN,
    session: requests.Session | None = None,
    sleeper=time.sleep, rng: random.Random | Any = random,
    dry_run: bool = False,
) -> dict:
    raw_dir = raw_dir or default_raw_dir()
    session = session or requests.Session()
    budget = RequestBudget(max_requests)
    written = 0
    skipped = 0

    for asin in targets:
        if budget.exhausted():
            logger.info("request budget (%d) reached — stopping", max_requests)
            break
        out_path = raw_dir / f"{asin}.json"
        if is_fresh(out_path, refresh_days):
            logger.info("%s: fresh (<%dd) skip", asin, refresh_days)
            continue
        if dry_run:
            logger.info("[dry-run] would crawl %s", asin)
            continue
        payload = crawl_asin(
            asin, matched_path=matched_path, session=session, budget=budget,
            sleeper=sleeper, rng=rng, max_pages=max_pages,
        )
        if payload is None:
            skipped += 1
            continue
        raw_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        logger.info("%s: wrote %s (%d reviews)", asin, out_path, len(payload["reviews"]))

    summary = {
        "targets": len(targets), "written": written, "skipped": skipped,
        "requests_made": budget.consumed,
    }
    logger.info("done: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asins", default="", help="対象 ASIN をカンマ区切りで明示指定")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help="日次リクエスト上限")
    ap.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="取得せず対象/計画のみ表示")
    args = ap.parse_args()

    asins = [a.strip() for a in args.asins.split(",") if a.strip()] or None
    targets = select_targets(limit=args.limit, asins=asins)
    logger.info("対象 %d ASIN: %s", len(targets), targets)

    run(
        targets,
        max_requests=args.max_requests,
        refresh_days=args.refresh_days,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
