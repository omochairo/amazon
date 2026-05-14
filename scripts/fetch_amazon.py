import os
import re
import json
import sys
import logging
import argparse
from datetime import datetime, timezone
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
        if isinstance(cur, dict):
            cur = cur.get(a)
        else:
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

def extract_availability(item: dict) -> str:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        msg = _safe_get(listings[0], "availability", "message")
        if isinstance(msg, str):
            return msg.strip()
    return ""

def extract_loyalty_points(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        pts = _safe_get(listings[0], "loyaltyPoints", "points")
        try:
            return int(pts) if pts is not None else 0
        except (TypeError, ValueError):
            return 0
    return 0

def write_per_asin_snapshot(out_root: str, item: dict) -> None:
    """Persist per-ASIN amazon snapshot so build_post.py can back-fill badge
    fields for past articles even after data/raw/amazon.json gets overwritten.
    """
    asin = item.get("asin")
    if not asin:
        return
    per_asin_dir = os.path.join(out_root, "per_asin", asin)
    try:
        os.makedirs(per_asin_dir, exist_ok=True)
        snapshot = {
            "asin": asin,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "item": item,
        }
        with open(os.path.join(per_asin_dir, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Failed to write per_asin snapshot for {asin}: {e}")


def write_per_asin_competitors(out_root: str, target_asin: str, items: list, max_n: int = 5) -> None:
    """Save real Amazon search hits as competitor candidates for the target ASIN.

    Jules hallucinates competitor ASINs in ``competitive_analysis[]``; this file
    lets ``build_post.py`` replace those with API-verified items so the
    Amazonで見る button and product image always resolve to a real listing.
    """
    if not target_asin:
        return
    competitors = []
    for it in items:
        a = it.get("asin")
        if not a or a == target_asin:
            continue
        if not it.get("image"):
            continue
        competitors.append({
            "asin": a,
            "name": it.get("title") or "",
            "image": it.get("image"),
            "price": it.get("price") or 0,
            "url": it.get("url") or f"https://www.amazon.co.jp/dp/{a}/",
            "features": (it.get("features") or [])[:3],
        })
        if len(competitors) >= max_n:
            break
    if not competitors:
        return
    per_asin_dir = os.path.join(out_root, "per_asin", target_asin)
    try:
        os.makedirs(per_asin_dir, exist_ok=True)
        payload = {
            "asin": target_asin,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "competitors": competitors,
        }
        with open(os.path.join(per_asin_dir, "competitors.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Wrote {len(competitors)} competitors for {target_asin}")
    except OSError as e:
        logger.warning(f"Failed to write competitors for {target_asin}: {e}")


def _load_existing_article_asins(articles_dir: str = "data/articles") -> set:
    """Return the set of ASINs that already have an article.

    Used to strip already-covered ASINs from amazon.json before Jules reads
    it, so the AI can't pick a duplicate even if it ignores the textual
    "no duplicate ASIN" rule in the prompt. Falls back to the slug suffix
    when ``product.asin`` is missing.
    """
    asins: set = set()
    if not os.path.isdir(articles_dir):
        return asins
    pattern = re.compile(r"-(B0[A-Z0-9]{8})$")
    for name in os.listdir(articles_dir):
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        if stem.endswith((".enrichment", ".seo", ".quality")):
            continue
        path = os.path.join(articles_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = None
        a = None
        if isinstance(data, dict):
            prod = data.get("product") if isinstance(data.get("product"), dict) else None
            if prod and prod.get("asin"):
                a = prod["asin"]
        if not a:
            m = pattern.search(stem)
            if m:
                a = m.group(1)
        if a:
            asins.add(a)
    return asins


def extract_savings_percentage(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        pct = _safe_get(listings[0], "price", "savings", "percentage")
        try:
            return int(pct) if pct is not None else 0
        except (TypeError, ValueError):
            return 0
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="daily_random")
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--articles-dir", default="data/articles",
                        help="Directory scanned for already-published ASINs; matching items are excluded from amazon.json so Jules cannot pick a duplicate")
    args = parser.parse_args()

    app_id = get_secret("AMAZON_CREATORS_APPLICATION_ID")
    cid = get_secret("AMAZON_CREATORS_CREDENTIAL_ID")
    cs = get_secret("AMAZON_CREATORS_CREDENTIAL_SECRET")
    tag = get_secret("AMAZON_PARTNER_TAG")

    items = []

    if not app_id or not cid or not cs or not tag or not HAS_CREATORS_API:
        logger.warning("Amazon API keys or module missing. Skipping Amazon fetch (returning empty data).")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": [], "mode": args.mode}, f, ensure_ascii=False, indent=4)
        return

    api = CreatorsAPIClient()

    resources = [
        "images.primary.large",
        "itemInfo.title",
        "itemInfo.features",
        "offersV2.listings.price",
        "offersV2.listings.availability",
        "offersV2.listings.loyaltyPoints",
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
                    "availability": extract_availability(it),
                    "loyalty_points": extract_loyalty_points(it),
                    "savings_percentage": extract_savings_percentage(it),
                    "source": "Amazon (Target)"
                })
        except Exception as e:
            logger.error(f"Failed to fetch target ASIN: {e}")
            sys.exit(1)

    # Search Mode: Complement with related products
    search_kw = args.keyword
    if not search_kw:
        search_kw = items[0]["title"][:20] if (args.asin and items) else "知育玩具"

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
                "availability": extract_availability(it),
                "loyalty_points": extract_loyalty_points(it),
                "savings_percentage": extract_savings_percentage(it),
                "source": "Amazon"
            })
    except Exception as e:
        logger.error(f"Search failed: {e}")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    # Strip already-covered ASINs from the Jules-facing list. Sniper-mode
    # target ASIN is exempt (user explicitly asked to re-fetch it). Snapshots
    # and per-ASIN competitor files still get every item so that internal
    # linking and badge back-fill keep working for the full catalog.
    existing = _load_existing_article_asins(args.articles_dir)
    target_asin = args.asin or None
    items_for_jules = [
        it for it in items
        if it.get("asin") == target_asin or it.get("asin") not in existing
    ]
    dropped = len(items) - len(items_for_jules)
    if dropped:
        logger.info(
            f"Excluded {dropped} item(s) already covered by data/articles/ "
            f"({len(items_for_jules)} remain for Jules)"
        )

    with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": search_kw, "items": items_for_jules, "mode": args.mode}, f, ensure_ascii=False, indent=4)

    for it in items:
        write_per_asin_snapshot(args.out, it)

    if args.asin:
        write_per_asin_competitors(args.out, args.asin, items)

if __name__ == "__main__":
    main()
