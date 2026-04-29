import os
import json
import sys
import logging
import argparse
from typing import Any, Optional

def get_secret(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"[FATAL] {name} not set. This script MUST run on GitHub Actions.\n")
        sys.exit(2)
    return v

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
    return _safe_get(item, "item_info", "features", "display_values", default=[])

def extract_price(item: Any) -> int:
    listings = _safe_get(item, "offers", "listings", default=None)
    if listings:
        price_val = _safe_get(listings[0], "price", "amount", default=0)
        return int(price_val)
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/products/")
    args = parser.parse_args()

    cid = get_secret("AMAZON_ACCESS_KEY")
    cs = get_secret("AMAZON_SECRET_KEY")
    tag = get_secret("AMAZON_ASSOC_TAG")

    api = AmazonCreatorsApi(cid, cs, "2.3", tag, Country.JP)

    items = []

    if args.asin:
        logger.info(f"Fetching target ASIN: {args.asin}")
        try:
            res = api.get_items(item_ids=[args.asin])
            for it in getattr(res, "items", []):
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
            logger.warning(f"ASIN lookup failed: {e}")

    search_kw = args.keyword if args.keyword else (items[0]["title"][:20] if items else "知育玩具")
    logger.info(f"Searching for competitors: {search_kw}")

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
    filename = args.asin if args.asin else "search_result"
    out_path = os.path.join(args.out, f"{filename}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"keyword": search_kw, "items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Data saved to {out_path}")

if __name__ == "__main__":
    main()
