import os
import json
import sys
import logging
import argparse
from typing import Any, Optional

def get_secret(name: str) -> str:
    return os.environ.get(name)

try:
    from amazon_creatorsapi import AmazonCreatorsApi, Country
    from amazon_creatorsapi.errors import AmazonCreatorsApiError
except ImportError:
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon")

def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    cur = obj
    for a in attrs:
        if cur is None: return default
        cur = getattr(cur, a, None)
    return cur if cur is not None else default

def extract_features(item: Any) -> list:
    features = _safe_get(item, "item_info", "features", "display_values", default=[])
    return features

def extract_price(item: Any) -> int:
    listings = _safe_get(item, "offers", "listings", default=None)
    if listings:
        price_val = _safe_get(listings[0], "price", "amount", default=0)
        return int(price_val)
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="daily_random")
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    cid = get_secret("PAAPI_ACCESS_KEY")
    cs = get_secret("PAAPI_SECRET_KEY")
    tag = get_secret("PAAPI_PARTNER_TAG")

    items = []

    if not cid or not cs:
        logger.warning("Amazon API keys missing. Generating mock test data for Amazon.")
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

    api = AmazonCreatorsApi(cid, cs, "2.3", tag, Country.JP)

    # Sniper Mode: Fetch specific ASIN first
    if args.asin:
        logger.info(f"Sniper Mode: Fetching ASIN {args.asin}")
        try:
            # SDK might have get_items for specific ASINs
            res = api.get_items(item_ids=[args.asin])
            for it in getattr(res, "items", []):
                items.append({
                    "asin": it.asin,
                    "title": _safe_get(it, "item_info", "title", "display_value"),
                    "price": extract_price(it),
                    "features": extract_features(it),
                    "url": f"https://www.amazon.co.jp/dp/{it.asin}/?tag={tag}",
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
        res = api.search_items(keywords=search_kw, item_count=10)
        for it in getattr(res, "items", []):
            if any(i["asin"] == it.asin for i in items): continue
            items.append({
                "asin": it.asin,
                "title": _safe_get(it, "item_info", "title", "display_value"),
                "price": extract_price(it),
                "features": extract_features(it),
                "url": f"https://www.amazon.co.jp/dp/{it.asin}/?tag={tag}",
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
