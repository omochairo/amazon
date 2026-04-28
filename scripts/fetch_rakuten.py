"""
fetch_rakuten.py
楽天市場 商品検索API (Rakuten Ichiba Item Search API) でキーワード検索し、
アフィリエイトリンク付き JSON を生成する。

【2026年4月時点の仕様】
- 旧ドメイン (app.rakuten.co.jp) は 2026年5月14日に完全廃止
- 新ドメイン (openapi.rakuten.co.jp) + 新APIバージョンへの移行が必要
- accessKey は Authorization: Bearer ヘッダで渡すのが推奨
- 本スクリプトは現行最新版 version:2022-06-01 を使用

必要な環境変数 (GitHub Secrets):
  RAKUTEN_APP_ID         : Application ID (applicationId)
  RAKUTEN_ACCESS_KEY     : Access Key (新システム必須)
  RAKUTEN_AFFILIATE_ID   : アフィリエイトID

任意の環境変数:
  RAKUTEN_HITS           : 取得件数 (1〜30、デフォルト: 5)
  RAKUTEN_SORT           : ソート順 (デフォルト: standard)
                           例: -reviewCount / +itemPrice / -itemPrice / standard
"""

import os
import json
import sys
import logging
from typing import Any, Optional

import requests


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
# 楽天市場 商品検索API (現行最新版、新ドメイン)
# https://webservice.rakuten.co.jp/documentation/ichiba-item-search
RAKUTEN_API_URL = (
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
)

REQUEST_TIMEOUT = 15  # 秒

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch_rakuten")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def _get_int_env(name: str, default: int, lo: int, hi: int) -> int:
    """環境変数を整数として取得し、範囲内に丸める。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("Invalid value for %s: %r (using default %s)", name, raw, default)
        return default
    return max(lo, min(v, hi))


def _pick_image(item: dict) -> Optional[str]:
    """商品画像 URL を 1 つ取り出す。medium → small の順で優先。"""
    for key in ("mediumImageUrls", "smallImageUrls"):
        urls = item.get(key) or []
        if not urls:
            continue
        # formatVersion=1 では [{"imageUrl": "..."}, ...]
        # formatVersion=2 では ["...", ...]
        first = urls[0]
        if isinstance(first, dict):
            url = first.get("imageUrl")
        else:
            url = first
        if url:
            return url
    return None


def _normalize_item(raw: Any) -> Optional[dict]:
    """
    楽天API のレスポンスから 1 件分を取り出して整形する。
    formatVersion=1 / formatVersion=2 両対応。
    """
    if isinstance(raw, dict) and "Item" in raw:
        # formatVersion=1: {"Item": {...}}
        item = raw["Item"]
    elif isinstance(raw, dict):
        # formatVersion=2: {...} (フラット)
        item = raw
    else:
        return None

    title = item.get("itemName")
    if not title:
        return None

    # アフィリエイトURL優先、なければ通常の itemUrl
    url = item.get("affiliateUrl") or item.get("itemUrl")
    if not url:
        return None

    return {
        "title": title,
        "price": item.get("itemPrice"),
        "url": url,
        "image": _pick_image(item),
        "shop": item.get("shopName") or "Rakuten",
        "itemCode": item.get("itemCode"),
        "reviewCount": item.get("reviewCount"),
        "reviewAverage": item.get("reviewAverage"),
        "source": "Rakuten",
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def fetch_rakuten(keyword: str) -> None:
    # --- 認証情報 ----------------------------------------------------------
    application_id = os.environ.get("RAKUTEN_APP_ID")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY")
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID")

    if not application_id:
        logger.error("RAKUTEN_APP_ID が未設定です。")
        sys.exit(1)
    if not access_key:
        # 新システムでは accessKey が必須
        logger.error(
            "RAKUTEN_ACCESS_KEY が未設定です。"
            "新ドメイン (openapi.rakuten.co.jp) では accessKey が必須です。"
        )
        sys.exit(1)
    if not affiliate_id:
        logger.warning(
            "RAKUTEN_AFFILIATE_ID が未設定です。affiliateUrl が返らないため "
            "通常の itemUrl をリンクに使用します。"
        )

    # --- リクエストパラメータ ---------------------------------------------
    hits = _get_int_env("RAKUTEN_HITS", default=5, lo=1, hi=30)
    sort = os.environ.get("RAKUTEN_SORT", "standard")

    params: dict[str, Any] = {
        "applicationId": application_id,
        "keyword": keyword,
        "format": "json",
        "formatVersion": 2,   # フラット形式で扱いやすい
        "hits": hits,
        "sort": sort,
        "availability": 1,    # 在庫ありのみ
        "imageFlag": 1,       # 画像ありのみ
    }
    if affiliate_id:
        params["affiliateId"] = affiliate_id

    # accessKey はパラメータでもヘッダーでも可。公式は Authorization ヘッダ推奨。
    headers = {
        "Authorization": f"Bearer {access_key}",
        "Accept": "application/json",
    }

    logger.info("--- Rakuten Ichiba Fetch ---")
    logger.info("Endpoint  : %s", RAKUTEN_API_URL)
    logger.info("Keyword   : %s", keyword)
    logger.info("Hits      : %s", hits)
    logger.info("Sort      : %s", sort)

    # --- API 呼び出し ------------------------------------------------------
    try:
        resp = requests.get(
            RAKUTEN_API_URL,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.error("HTTP request failed: %s", e)
        sys.exit(1)

    # 楽天APIはエラーを HTTP ステータス + JSON ボディで返す
    if resp.status_code != 200:
        try:
            err = resp.json()
            logger.error(
                "Rakuten API error (HTTP %s): %s - %s",
                resp.status_code,
                err.get("error"),
                err.get("error_description"),
            )
        except ValueError:
            logger.error(
                "Rakuten API error (HTTP %s): %s",
                resp.status_code,
                resp.text[:500],
            )
        sys.exit(1)

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("Failed to parse JSON response: %s", e)
        sys.exit(1)

    # --- レスポンス整形 ----------------------------------------------------
    raw_items = data.get("Items", []) or []
    items: list[dict] = []
    for raw in raw_items:
        normalized = _normalize_item(raw)
        if normalized is None:
            continue
        logger.info("Product : %s", normalized["title"])
        logger.info("  URL   : %s", normalized["url"])
        logger.info("  Price : %s", normalized["price"])
        items.append(normalized)

    if not items:
        logger.warning("No items found for keyword: %s", keyword)

    results = {
        "keyword": keyword,
        "count": data.get("count"),
        "page": data.get("page"),
        "items": items,
    }

    # --- 保存 -------------------------------------------------------------
    os.makedirs("data", exist_ok=True)
    out_path = "data/rakuten_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=4)

    logger.info("Saved %d items to %s", len(items), out_path)


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1:
        fetch_rakuten(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_rakuten.py <keyword>")
        sys.exit(1)
