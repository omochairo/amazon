"""
fetch_reviews.py
楽天 IchibaItemReview API と Yahoo!ショッピング reviewSearch API から
商品レビュー本文を取得し、ASIN ベースで data/raw/reviews.json に保存する。

入力:
  data/raw/rakuten_matched.json — Amazon ASIN ↔ 楽天 itemCode のマッチ結果
  data/raw/yahoo_matched.json   — Amazon ASIN ↔ Yahoo url のマッチ結果

出力:
  data/raw/reviews.json = {
    "<ASIN>": {
      "rakuten": [{"rating": int, "title": str, "body": str, "posted_at": str}],
      "yahoo":   [{"rating": int, "title": str, "body": str, "posted_at": str}]
    }, ...
  }

必要な環境変数 (GitHub Secrets):
  RAKUTEN_APP_ID    — 楽天 WebService applicationId (legacy /services/api 用)
  YAHOO_CLIENT_ID   — Yahoo! デベロッパー Client ID

API キーが無い場合は空ファイルを書き出して終了する (fail-soft)。
楽天 WebService API は段階的廃止予定なので、失敗しても Yahoo 側の取得は継続する。
"""

import json
import logging
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_reviews")

RAKUTEN_REVIEW_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Review/20140222"
YAHOO_REVIEW_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V1/json/reviewSearch"
REQUEST_TIMEOUT = 15
TOP_N_PER_SITE = 5  # ASIN あたり各サイトから最大 N 件
SLEEP_SEC = 1.0     # API レート制限対策 (1リクエスト/秒)


def _load_json(path: str) -> Any:
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("input missing: %s", path)
        return None


def fetch_rakuten_reviews(app_id: str, item_code: str) -> list[dict]:
    """楽天 IchibaItem Review API でレビュー本文を取得。失敗時は []。"""
    params = {"applicationId": app_id, "itemCode": item_code, "hits": TOP_N_PER_SITE, "page": 1, "formatVersion": 2}
    try:
        r = requests.get(RAKUTEN_REVIEW_URL, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.info("rakuten review HTTP %s for itemCode=%s", r.status_code, item_code)
            return []
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.info("rakuten review fetch error itemCode=%s: %s", item_code, e)
        return []
    out = []
    for item in (data.get("Items") or [])[:TOP_N_PER_SITE]:
        out.append({
            "rating": item.get("itemRating"),
            "title": (item.get("reviewSubject") or "").strip(),
            "body": (item.get("reviewBody") or "").strip(),
            "posted_at": item.get("postDate", ""),
            "reviewer_age": item.get("userAge"),
            "reviewer_sex": item.get("userSex"),
        })
    return out


def _extract_yahoo_jan_or_code(url: str) -> str | None:
    """Yahoo!ショッピング商品URLから storeId/srcCode を取り出す (reviewSearch の query 用)。"""
    # 例: https://store.shopping.yahoo.co.jp/dashing/irobb01.html → store=dashing, code=irobb01
    m = re.search(r"shopping\.yahoo\.co\.jp/([^/]+)/([^/?]+?)(?:\.html)?(?:[?&]|$)", url)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return None


def fetch_yahoo_reviews(app_id: str, jan_or_code: str) -> list[dict]:
    """Yahoo!ショッピング reviewSearch API でレビュー本文取得。失敗時は []。"""
    params = {"appid": app_id, "jan": jan_or_code, "results": TOP_N_PER_SITE, "output": "json"}
    try:
        r = requests.get(YAHOO_REVIEW_URL, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.info("yahoo review HTTP %s for %s", r.status_code, jan_or_code)
            return []
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.info("yahoo review fetch error %s: %s", jan_or_code, e)
        return []
    out = []
    rs = (data.get("ResultSet") or {}).get("Result") or []
    if isinstance(rs, dict):
        rs = [rs]
    for item in rs[:TOP_N_PER_SITE]:
        out.append({
            "rating": item.get("Ratings", {}).get("Rate") if isinstance(item.get("Ratings"), dict) else None,
            "title": (item.get("ReviewTitle") or "").strip(),
            "body": (item.get("ReviewText") or item.get("Description") or "").strip(),
            "posted_at": item.get("PostedAt", ""),
        })
    return out


def main():
    out_dir = "data/raw"
    out_path = os.path.join(out_dir, "reviews.json")
    os.makedirs(out_dir, exist_ok=True)

    rakuten_app = os.environ.get("RAKUTEN_APP_ID", "").strip()
    yahoo_app = os.environ.get("YAHOO_CLIENT_ID", "").strip()
    if not rakuten_app and not yahoo_app:
        logger.warning("No API keys (RAKUTEN_APP_ID / YAHOO_CLIENT_ID). Writing empty reviews.json.")
        json.dump({}, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    rakuten_matched = _load_json("data/raw/rakuten_matched.json") or {}
    yahoo_matched = _load_json("data/raw/yahoo_matched.json") or {}
    r_items = rakuten_matched.get("items", []) if isinstance(rakuten_matched, dict) else []
    y_items = yahoo_matched.get("items", []) if isinstance(yahoo_matched, dict) else []

    by_asin: dict[str, dict] = {}
    if rakuten_app:
        for it in r_items:
            asin = it.get("matched_asin")
            code = it.get("itemCode")
            if not asin or not code:
                continue
            revs = fetch_rakuten_reviews(rakuten_app, code)
            if revs:
                by_asin.setdefault(asin, {})["rakuten"] = revs
            time.sleep(SLEEP_SEC)
    else:
        logger.info("RAKUTEN_APP_ID missing, skipping rakuten reviews.")

    if yahoo_app:
        for it in y_items:
            asin = it.get("matched_asin")
            url = it.get("url", "")
            key = _extract_yahoo_jan_or_code(url) if url else None
            if not asin or not key:
                continue
            revs = fetch_yahoo_reviews(yahoo_app, key)
            if revs:
                by_asin.setdefault(asin, {})["yahoo"] = revs
            time.sleep(SLEEP_SEC)
    else:
        logger.info("YAHOO_CLIENT_ID missing, skipping yahoo reviews.")

    json.dump(by_asin, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    logger.info("reviews.json written: %d ASIN entries", len(by_asin))


if __name__ == "__main__":
    main()
