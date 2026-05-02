import os
import json
import sys
import logging
import argparse
from typing import Any, Optional

def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon")

HAS_CREATORS_API = False
try:
    from creators_api_client import CreatorsAPIClient
    HAS_CREATORS_API = True
except ImportError as e:
    logger.warning(f"creators_api_client module not found or import failed: {e}. Falling back to mock data generation.")

def _safe_get(obj: dict, *attrs: str, default: Any = None) -> Any:
    cur = obj
    for a in attrs:
        if cur is None: return default
        cur = getattr(cur, a, None)
    return cur if cur is not None else default

def extract_features(item: dict) -> list:
    return _safe_get(item, "itemInfo", "features", "displayValues", default=[])

def extract_price(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings and len(listings) > 0:
        money = _safe_get(listings[0], "price", "money", default={})
        return int(money.get("amount", 0))
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="daily_random")
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    app_id = get_secret("AMAZON_CREATORS_APPLICATION_ID")
    cid = get_secret("AMAZON_CREATORS_CREDENTIAL_ID")
    cs = get_secret("AMAZON_CREATORS_CREDENTIAL_SECRET")
    tag = get_secret("AMAZON_PARTNER_TAG")

    items = []

    if not app_id or not cid or not cs or not tag or not HAS_CREATORS_API:
        logger.warning("Amazon API keys or module missing. Generating mock test data for Amazon.")
        os.makedirs(args.out, exist_ok=True)
        items = [{
            "asin": "MOCK_AMZN_001",
            "title": "[テストデータ] モック知育玩具ブロック",
            "price": 3500,
            "features": ["テスト特徴1", "テスト特徴2"],
            "url": "https://www.amazon.co.jp/dp/MOCK_AMZN_001/?tag=mock-22",
            "image": "https://via.placeholder.com/300x300.png?text=Amazon+Mock",
            "source": "Amazon (Mock)"
        }]
        with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": items, "mode": args.mode}, f, ensure_ascii=False, indent=4)
        return

    api = CreatorsAPIClient()

    resources = [
        "images.primary.large",
        "itemInfo.title",
        "itemInfo.features",
        "offersV2.listings.price"
    ]

    # Sniper Mode: Fetch specific ASIN first
    if args.asin:
        logger.info(f"Sniper Mode: Fetching ASIN {args.asin}")
        try:
            res = api.get_items([args.asin], resources=resources)
            found_items = _safe_get(res, "itemsResult", "items", default=[])
            for it in found_items:
                asin = it.get("asin")
                items.append({
                    "asin": asin,
                    "title": _safe_get(it, "itemInfo", "title", "displayValue"),
                    "price": extract_price(it),
                    "features": extract_features(it),
                    "url": f"https://www.amazon.co.jp/dp/{asin}/?tag={tag}",
                    "image": _safe_get(it, "images", "primary", "large", "url"),
                    "source": "Amazon (Target)"
                })
        except Exception as e:
            logger.warning(f"Failed to fetch target ASIN: {e}")

    # Search Mode: Complement with related products
    search_kw = args.keyword
    if args.asin and not search_kw:
        # Use first item title as keyword if possible, else generic
        search_kw = items[0]["title"][:20] if items else "知育玩具"

    logger.info(f"Search Mode: Keyword '{search_kw}'")
    try:
        res = api.search_items(keywords=search_kw, search_index="All", resources=resources)
        found_items = _safe_get(res, "searchResult", "items", default=[])
        for it in found_items:
            asin = it.get("asin")
            if any(i["asin"] == asin for i in items): continue
            items.append({
                "asin": asin,
                "title": _safe_get(it, "itemInfo", "title", "displayValue"),
                "price": extract_price(it),
                "features": extract_features(it),
                "url": f"https://www.amazon.co.jp/dp/{asin}/?tag={tag}",
                "image": _safe_get(it, "images", "primary", "large", "url"),
                "source": "Amazon"
            })
    except Exception as e:
        logger.error(f"Search failed: {e}")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": search_kw, "items": items, "mode": args.mode}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
