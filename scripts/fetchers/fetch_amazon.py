import os
import json
import sys
import logging
from typing import Any, Optional

def get_secret(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(
            f"[FATAL] {name} not set. "
            f"This script MUST run on GitHub Actions, not in Jules sandbox.\n"
        )
        sys.exit(2)
    return v

try:
    from amazon_creatorsapi import AmazonCreatorsApi, Country
    from amazon_creatorsapi.errors import AmazonCreatorsApiError
except ImportError:
    # On Actions we need this installed
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon")

COUNTRY_DOMAIN = {"JP": "amazon.co.jp"}
REGION_VERSION = {"JP": "2.3"}

def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    cur = obj
    for a in attrs:
        if cur is None: return default
        cur = getattr(cur, a, None)
    return cur if cur is not None else default

def extract_price(item: Any) -> str:
    listings = _safe_get(item, "offers", "listings", default=None)
    if listings:
        price = _safe_get(listings[0], "price", "display_amount", default=None)
        if price: return price
    return "N/A"

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="daily_random")
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    credential_id = get_secret("AMAZON_ACCESS_KEY")
    credential_secret = get_secret("AMAZON_SECRET_KEY")
    affiliate_tag = get_secret("AMAZON_ASSOC_TAG")

    api = AmazonCreatorsApi(
        credential_id=credential_id,
        credential_secret=credential_secret,
        version="2.3",
        tag=affiliate_tag,
        country=Country.JP,
    )

    keyword = args.keyword
    if args.asin:
        # Search by ASIN logic would go here if SDK supports it, or just generic search
        keyword = args.asin

    try:
        search_result = api.search_items(keywords=keyword, item_count=10)
    except Exception as e:
        logger.error(f"Amazon API error: {e}")
        sys.exit(1)

    items = []
    raw_items = getattr(search_result, "items", None) or []
    for item in raw_items:
        asin = getattr(item, "asin", None)
        items.append({
            "asin": asin,
            "title": _safe_get(item, "item_info", "title", "display_value", default="No Title"),
            "price": extract_price(item),
            "url": f"https://www.amazon.co.jp/dp/{asin}/?tag={affiliate_tag}",
            "image": _safe_get(item, "images", "primary", "large", "url"),
            "source": "Amazon"
        })

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": keyword, "items": items}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
