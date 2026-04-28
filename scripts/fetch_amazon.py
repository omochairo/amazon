"""
fetch_amazon.py
Amazon Creators API (旧 PA-API 5.0 の後継) でキーワード検索し、
アフィリエイトリンク付き JSON を生成する。

PA-API 5.0 は 2026年4月30日に廃止されるため、本スクリプトは新しい
Amazon Creators API (OAuth 2.0 ベース) で動作する。

必要な環境変数 (GitHub Secrets):
  AMAZON_ACCESS_KEY    : Creators API の Credential ID
                         (Associates Central → Tools → Creators API で発行)
  AMAZON_SECRET_KEY    : Creators API の Credential Secret
  AMAZON_PARTNER_TAG   : アフィリエイトタグ (例: yourtag-22)

任意の環境変数:
  AMAZON_COUNTRY            : 国コード (デフォルト: JP)
  AMAZON_API_VERSION        : Credential Version
                              省略時は国に応じて自動 (NA=2.1 / EU=2.2 / FE=2.3)
  AMAZON_SEARCH_ITEM_COUNT  : 取得件数 (1〜10、デフォルト: 10)

注意:
  Creators API は OAuth 2.0 + 新しい Credential ID/Secret を使用するため、
  旧 PA-API 5.0 の Access Key / Secret Key は使用できません。
  GitHub Secrets の AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY には、
  Creators API の Credential ID / Credential Secret を設定してください。
"""

import os
import json
import sys
import logging
from typing import Any, Optional

try:
    from amazon_creatorsapi import AmazonCreatorsApi, Country
    from amazon_creatorsapi.errors import AmazonException
except ImportError:
    print(
        "Error: 'amazon_creatorsapi' モジュールがインストールされていません。\n"
        "  pip install python-amazon-paapi\n"
        "  (パッケージ名は python-amazon-paapi ですが、import 名は amazon_creatorsapi です)",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch_amazon")


# ---------------------------------------------------------------------------
# 国 / リージョン設定
# ---------------------------------------------------------------------------
# 国コード -> Amazon ドメイン (アフィリエイトリンク生成に使用)
COUNTRY_DOMAIN = {
    "JP": "amazon.co.jp",
    "US": "amazon.com",
    "UK": "amazon.co.uk",
    "GB": "amazon.co.uk",
    "DE": "amazon.de",
    "FR": "amazon.fr",
    "IT": "amazon.it",
    "ES": "amazon.es",
    "CA": "amazon.ca",
    "AU": "amazon.com.au",
    "IN": "amazon.in",
    "MX": "amazon.com.mx",
    "BR": "amazon.com.br",
    "SG": "amazon.sg",
    "AE": "amazon.ae",
    "NL": "amazon.nl",
    "SE": "amazon.se",
    "PL": "amazon.pl",
    "TR": "amazon.com.tr",
    "SA": "amazon.sa",
    "BE": "amazon.com.be",
    "EG": "amazon.eg",
}

# Creators API の Credential Version (リージョン別)
# https://pypi.org/project/amazon-creatorsapi-python-sdk/ の仕様より
#   NA (北米)   = "2.1"
#   EU (欧州・印度ほか) = "2.2"
#   FE (極東)   = "2.3"
REGION_VERSION = {
    # FE region
    "JP": "2.3",
    "AU": "2.3",
    "SG": "2.3",
    # NA region
    "US": "2.1",
    "CA": "2.1",
    "MX": "2.1",
    "BR": "2.1",
    # EU region
    "UK": "2.2", "GB": "2.2",
    "DE": "2.2", "FR": "2.2", "IT": "2.2", "ES": "2.2",
    "IN": "2.2", "NL": "2.2", "SE": "2.2", "PL": "2.2",
    "TR": "2.2", "AE": "2.2", "SA": "2.2", "BE": "2.2", "EG": "2.2",
}


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------
def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    """ネストした属性を安全に取り出す。途中が None でも例外を出さない。"""
    cur = obj
    for a in attrs:
        if cur is None:
            return default
        cur = getattr(cur, a, None)
    return cur if cur is not None else default


def build_affiliate_url(asin: str, tag: str, domain: str) -> str:
    """正規のアフィリエイトリンクを生成する。形式: https://www.{domain}/dp/{ASIN}/?tag={TAG}"""
    return f"https://www.{domain}/dp/{asin}/?tag={tag}"


def validate_url(url: str) -> bool:
    """生成された URL の最終チェック。"""
    return (
        isinstance(url, str)
        and url.startswith("https://www.amazon.")
        and "/dp/" in url
        and "tag=" in url
    )


def extract_price(item: Any) -> str:
    """
    価格を取り出す。Creators API では OffersV2 が標準だが、SDK のラッパーは
    item.offers.listings[0].price.display_amount で旧来通りアクセスできるため、
    まずそちらを試し、ダメなら OffersV2 を見る。
    """
    # 1) 旧来形式 (SDK が互換ラップしている場合)
    listings = _safe_get(item, "offers", "listings", default=None)
    if listings:
        first = listings[0] if len(listings) > 0 else None
        price = _safe_get(first, "price", "display_amount", default=None)
        if price:
            return price

    # 2) OffersV2 形式
    listings_v2 = _safe_get(item, "offers_v2", "listings", default=None)
    if listings_v2:
        first = listings_v2[0] if len(listings_v2) > 0 else None
        price = (
            _safe_get(first, "price", "money", "display_amount", default=None)
            or _safe_get(first, "price", "display_amount", default=None)
        )
        if price:
            return price

    return "N/A"


def extract_item_data(item: Any, affiliate_tag: str, domain: str) -> Optional[dict]:
    """API レスポンスの item 1 件分を辞書化する。失敗時は None。"""
    asin = getattr(item, "asin", None)
    if not asin:
        return None

    affiliate_url = build_affiliate_url(asin, affiliate_tag, domain)
    if not validate_url(affiliate_url):
        logger.warning("Invalid URL generated for ASIN %s", asin)
        return None

    title = _safe_get(item, "item_info", "title", "display_value", default="No Title")
    price = extract_price(item)
    image_url = _safe_get(item, "images", "primary", "large", "url", default=None)

    return {
        "asin": asin,
        "title": title,
        "price": price,
        "image": image_url,
        "url": affiliate_url,
        "source": "Amazon",
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main(keyword: str) -> None:
    # --- 認証情報 ----------------------------------------------------------
    # GitHub Secrets の既存名 (AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY) を維持。
    # ただし中身は Creators API の Credential ID / Credential Secret を入れる必要あり。
    credential_id = os.getenv("AMAZON_ACCESS_KEY")
    credential_secret = os.getenv("AMAZON_SECRET_KEY")
    affiliate_tag = (
        os.getenv("AMAZON_PARTNER_TAG") or os.getenv("AMAZON_AFFILIATE_TAG")
    )
    country_code = os.getenv("AMAZON_COUNTRY", "JP").upper()

    if not all([credential_id, credential_secret, affiliate_tag]):
        logger.error(
            "AMAZON_ACCESS_KEY (=Credential ID), AMAZON_SECRET_KEY (=Credential Secret), "
            "AMAZON_PARTNER_TAG をすべて設定してください。"
        )
        sys.exit(1)

    # --- 国 / バージョン解決 -----------------------------------------------
    country = getattr(Country, country_code, Country.JP)
    domain = COUNTRY_DOMAIN.get(country_code, "amazon.co.jp")
    api_version = (
        os.getenv("AMAZON_API_VERSION")
        or REGION_VERSION.get(country_code, "2.3")
    )

    # --- 取得件数 (Creators API は 1〜10) ---------------------------------
    try:
        item_count = int(os.getenv("AMAZON_SEARCH_ITEM_COUNT", "10"))
    except ValueError:
        item_count = 10
    item_count = max(1, min(item_count, 10))

    # --- API クライアント初期化 -------------------------------------------
    api = AmazonCreatorsApi(
        credential_id=credential_id,
        credential_secret=credential_secret,
        version=api_version,
        tag=affiliate_tag,
        country=country,
    )

    logger.info("--- Amazon Creators API Fetch ---")
    logger.info("Input Keyword     : %s", keyword)
    logger.info("Target Country    : %s", country_code)
    logger.info("Marketplace Domain: %s", domain)
    logger.info("Credential Version: %s", api_version)
    logger.info("Item Count        : %s", item_count)

    # --- 検索実行 ----------------------------------------------------------
    try:
        search_result = api.search_items(
            keywords=keyword,
            item_count=item_count,
        )
    except AmazonException as e:
        logger.error("Amazon Creators API error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error during API call: %s", e)
        sys.exit(1)

    raw_items = getattr(search_result, "items", None) or []
    if not raw_items:
        logger.warning("No items returned for keyword: %s", keyword)

    # --- 結果整形 ----------------------------------------------------------
    items: list[dict] = []
    for item in raw_items:
        data = extract_item_data(item, affiliate_tag, domain)
        if data is None:
            continue
        logger.info("Product : %s", data["title"])
        logger.info("  URL   : %s", data["url"])
        logger.info("  Price : %s", data["price"])
        items.append(data)

    results = {
        "keyword": keyword,
        "country": country_code,
        "items": items,
    }

    # --- 保存 (既存コードとの互換性のため両方の名前で出力) -----------------
    os.makedirs("data", exist_ok=True)
    for path in ("data/amazon_result.json", "data/search_result.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

    logger.info(
        "Saved %d items to data/amazon_result.json and data/search_result.json",
        len(items),
    )


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_amazon.py <keyword>")
        sys.exit(1)
