import os, sys, json, requests, logging

def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_rakuten")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    app_id = get_secret("RAKUTEN_APP_ID")
    access_key = get_secret("RAKUTEN_ACCESS_KEY")
    aff_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()

    if not app_id or not access_key:
        logger.warning("Rakuten API keys missing. Generating mock test data for Rakuten.")
        os.makedirs(args.out, exist_ok=True)
        items = [{
            "title": "[テストデータ] 楽天モック知育玩具ブロック",
            "price": 3500,
            "url": "https://hb.afl.rakuten.co.jp/hgc/mock/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fmock%2Fitem%2F",
            "image": "https://via.placeholder.com/300x300.png?text=Rakuten+Mock",
            "source": "Rakuten (Mock)"
        }]
        with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": items}, f, ensure_ascii=False, indent=4)
        return

    # --- Fetch Search Data (Layer 1) ---
    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    headers = {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon"
    }
    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": args.keyword,
        "genreId": "566382",
        "sort": "-updateTimestamp",
        "formatVersion": 2,
        "hits": 30
    }
    if aff_id: params["affiliateId"] = aff_id

    resp = requests.get(url, params=params, headers=headers)

    if resp.status_code != 200:
        logger.error(f"Rakuten RMS API failed ({url}): {resp.text}")
        sys.exit(1)

    data = resp.json()
    items = []
    # Note: New API might have a different JSON structure, but we assume it's still 'Items'
    raw_items = data.get("Items", [])
    if not isinstance(raw_items, list):
        logger.warning(f"Unexpected Rakuten Search API structure: 'Items' is {type(raw_items)}")
        raw_items = []

    for item in raw_items:
        i = item.get("Item", item) if isinstance(item, dict) else {}

        # Robust image extraction
        image_url = ""
        img_list = i.get("mediumImageUrls", [])
        if isinstance(img_list, list) and len(img_list) > 0:
            first_img = img_list[0]
            if isinstance(first_img, dict):
                image_url = first_img.get("imageUrl", "")
            elif isinstance(first_img, str):
                image_url = first_img

        items.append({
            "title": i.get("itemName", "Unknown Rakuten Product"),
            "price": i.get("itemPrice", 0),
            "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
            "image": image_url,
            "itemCode": i.get("itemCode", ""),
            "reviewCount": i.get("reviewCount", 0),
            "source": "Rakuten"
        })

    # --- Fetch Ranking Data (Layer 1) ---
    # Use standard Ichiba Ranking API (v1) if the RMS specific one is failing
    ranking_url = "https://app.rakuten.co.jp/services/api/IchibaItem/Ranking/20171001"
    ranking_params = {
        "applicationId": app_id,
        "genreId": "566382", # Toys genre
        "formatVersion": 2
    }
    if aff_id: ranking_params["affiliateId"] = aff_id

    try:
        rank_resp = requests.get(ranking_url, params=ranking_params, headers=headers)
        rank_items = []
        if rank_resp.status_code == 200:
            rank_data = rank_resp.json()
            raw_rank_items = rank_data.get("Items", [])
            if not isinstance(raw_rank_items, list):
                logger.warning(f"Unexpected Rakuten Ranking API structure: 'Items' is {type(raw_rank_items)}")
                raw_rank_items = []

            for item in raw_rank_items:
                i = item.get("Item", item) if isinstance(item, dict) else {}

                # Robust image extraction
                image_url = ""
                img_list = i.get("mediumImageUrls", [])
                if isinstance(img_list, list) and len(img_list) > 0:
                    first_img = img_list[0]
                    if isinstance(first_img, dict):
                        image_url = first_img.get("imageUrl", "")
                    elif isinstance(first_img, str):
                        image_url = first_img

                rank_items.append({
                    "rank": i.get("rank"),
                    "title": i.get("itemName", "Unknown Rank Product"),
                    "price": i.get("itemPrice", 0),
                    "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
                    "image": image_url,
                    "itemCode": i.get("itemCode", ""),
                    "reviewCount": i.get("reviewCount", 0)
                })

        with open(os.path.join(args.out, "rakuten_ranking.json"), "w", encoding="utf-8") as f:
            json.dump({"items": rank_items}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ranking API failed: {e}")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": args.keyword, "items": items}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
