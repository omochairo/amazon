"""
fetch_yahoo.py
Yahoo!ショッピング 商品検索API (V3) でキーワード検索し、
バリューコマース連携のアフィリエイトリンク付き JSON を生成する。

【2026年4月時点の仕様】
- エンドポイント: https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch
- アフィリエイト連携は Yahoo! API 公式パラメータ (affiliate_type=vc + affiliate_id) を使用
  → API 側で正規化された affiliateUrl が返るため、自前でURL組み立てるより安全
  → SID/PID は Yahoo! 側に渡す affiliate_id 文字列の中で利用する

必要な環境変数 (GitHub Secrets):
  YAHOO_CLIENT_ID        : Yahoo! デベロッパーアプリの Client ID (アプリケーションID)
  VALUECOMMERCE_SID      : バリューコマース サイトID
  VALUECOMMERCE_PID      : バリューコマース 広告スペースID(プログラムID)

任意の環境変数:
  YAHOO_RESULTS          : 取得件数 (デフォルト: 5、API上限は1リクエストあたり50)
  YAHOO_SORT             : ソート順 (デフォルト: -score)
                           例: -score / +price / -price / -review_count
  YAHOO_IMAGE_SIZE       : 取得画像サイズ (76/106/132/146/300/600、デフォルト: 300)

API利用制限:
  1クエリ/秒 (短時間の連続アクセスは制限される可能性あり)
"""

import os
import json
import sys
import logging
import urllib.parse
from typing import Any, Optional

import requests


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
YAHOO_API_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
REQUEST_TIMEOUT = 15  # 秒

# バリューコマースの referral URL ベース。
# Yahoo! API の affiliate_id にはこの URL を末尾「&vc_url=」付きで渡す仕様。
# https://developer.yahoo.co.jp/webapi/shopping/affiliate.html
VC_REFERRAL_BASE = "https://ck.jp.ap.valuecommerce.com/servlet/referral"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch_yahoo")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def build_vc_affiliate_id(sid: str, pid: str) -> str:
    """
    Yahoo!ショッピング API の affiliate_id パラメータに渡す文字列を構築する。
    公式仕様 (Yahoo!デベロッパーネットワーク) に従い、末尾は「&vc_url=」で終える。
    例: https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=XXX&pid=YYY&vc_url=
    """
    return f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="


def build_vc_affiliate_url_fallback(target_url: str, sid: str, pid: str) -> str:
    """
    API 側でアフィリエイト URL が返らなかった場合の保険として、
    自前で MyLink 形式のアフィリエイトリンクを組み立てる。
    """
    encoded = urllib.parse.quote(target_url, safe="")
    return f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url={encoded}"


def _get_int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("Invalid value for %s: %r (using default %s)", name, raw, default)
        return default
    return max(lo, min(v, hi))


def _pick_image(hit: dict, preferred_size: int) -> Optional[str]:
    """商品画像 URL を 1 つ取り出す。exImage(任意サイズ) → medium → small の順で優先。"""
    ex = hit.get("exImage") or {}
    if isinstance(ex, dict) and ex.get("url"):
        return ex["url"]

    image = hit.get("image") or {}
    if isinstance(image, dict):
        return image.get("medium") or image.get("small")
    return None


def _normalize_hit(hit: dict, sid: str, pid: str) -> Optional[dict]:
    """
    Yahoo! API のレスポンス hits[] 1 件分を整形する。
    URL は API が返したもの (affiliate_type=vc 指定時はアフィリエイト済) を使用。
    念のため、ドメインがバリューコマース経由になっていない場合は自前で変換する。
    """
    name = hit.get("name")
    raw_url = hit.get("url")
    if not name or not raw_url:
        return None

    # 既に API 側で affiliate_type=vc によりバリューコマース経由になっているかをチェック。
    # なっていなければフォールバックで自前変換する。
    if "valuecommerce.com" not in raw_url and sid and pid:
        affiliate_url = build_vc_affiliate_url_fallback(raw_url, sid, pid)
    else:
        affiliate_url = raw_url

    seller = hit.get("seller") or {}
    review = hit.get("review") or {}

    return {
        "title": name,
        "price": hit.get("price"),
        "url": affiliate_url,
        "image": _pick_image(hit, preferred_size=300),
        "shop": seller.get("name") or "Yahoo",
        "code": hit.get("code"),
        "janCode": hit.get("janCode"),
        "reviewCount": review.get("count"),
        "reviewAverage": review.get("rate"),
        "source": "Yahoo",
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def fetch_yahoo(keyword: str) -> None:
    # --- 認証情報 ----------------------------------------------------------
    client_id = os.environ.get("YAHOO_CLIENT_ID")
    sid = os.environ.get("VALUECOMMERCE_SID")
    pid = os.environ.get("VALUECOMMERCE_PID")

    if not client_id:
        logger.warning("YAHOO_CLIENT_ID missing. Skipping Yahoo fetch.")
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "yahoo_result.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump({"keyword": keyword, "items": []}, f, ensure_ascii=False, indent=4)
        return

    if not sid or not pid:
        logger.warning(
            "VALUECOMMERCE_SID または VALUECOMMERCE_PID が未設定です。"
            "アフィリエイトリンクは生成されず、通常の商品URLになります。"
        )

    # --- リクエストパラメータ ---------------------------------------------
    results_count = _get_int_env("YAHOO_RESULTS", default=5, lo=1, hi=50)
    sort = os.environ.get("YAHOO_SORT", "-score")
    image_size = _get_int_env("YAHOO_IMAGE_SIZE", default=300, lo=76, hi=600)

    params: dict[str, Any] = {
        "appid": client_id,
        "query": keyword,
        "results": results_count,
        "sort": sort,
        "image_size": image_size,
        "in_stock": "true",  # 在庫ありのみ
    }

    # 公式アフィリエイト連携: affiliate_type=vc + affiliate_id
    # Yahoo! API 側がバリューコマース経由のアフィリエイト URL を返してくれる
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = build_vc_affiliate_id(sid, pid)

    logger.info("--- Yahoo! Shopping V3 Fetch ---")
    logger.info("Endpoint     : %s", YAHOO_API_URL)
    logger.info("Keyword      : %s", keyword)
    logger.info("Results      : %s", results_count)
    logger.info("Sort         : %s", sort)
    logger.info("Affiliate    : %s", "vc (ValueCommerce)" if sid and pid else "OFF")

    # --- API 呼び出し ------------------------------------------------------
    try:
        response = requests.get(
            YAHOO_API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.error("HTTP request failed: %s", e)
        sys.exit(1)

    # 429 (Too Many Requests) など Yahoo API 固有のエラーをログ出力
    if response.status_code != 200:
        snippet = response.text[:500] if response.text else ""
        logger.error(
            "Yahoo Shopping API error (HTTP %s): %s",
            response.status_code,
            snippet,
        )
        if response.status_code == 429:
            logger.error("Rate limit exceeded (1 query/sec). Please retry after a moment.")
        sys.exit(1)

    try:
        data = response.json()
    except ValueError as e:
        logger.error("Failed to parse JSON response: %s", e)
        sys.exit(1)

    # --- レスポンス整形 ----------------------------------------------------
    hits = data.get("hits") or []
    items: list[dict] = []
    for hit in hits:
        normalized = _normalize_hit(hit, sid or "", pid or "")
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
        "totalResultsAvailable": data.get("totalResultsAvailable"),
        "totalResultsReturned": data.get("totalResultsReturned"),
        "items": items,
    }

    # --- 保存 (プロジェクトルートの data フォルダ) -------------------------
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "yahoo_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=4)

    logger.info("Saved %d items to %s", len(items), save_path)


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "知育玩具"
    fetch_yahoo(keyword)
