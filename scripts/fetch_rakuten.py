import os, sys, json, requests, logging

def get_secret(name: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v else v

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
        logger.warning("Rakuten API keys missing (app_id and access_key required). Skipping Rakuten fetch (returning empty data).")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": []}, f, ensure_ascii=False, indent=4)
        return

    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

    # Check if keyword is empty due to scheduled run defaults
    search_kw = args.keyword if args.keyword else "知育玩具"

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": search_kw,
        "formatVersion": 2,
        "hits": 10
    }
    if aff_id: params["affiliateId"] = aff_id

    # The 2026 API strictly checks Referer/Origin headers
    # The user must register this URL in the Rakuten App console
    headers = {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon"
    }

    resp = requests.get(url, params=params, headers=headers)
    if resp.status_code != 200:
        logger.error(f"Rakuten error: {resp.text}")
        sys.exit(1)

    data = resp.json()
    items = []
    for item in data.get("Items", []):
        items.append({
            "title": item.get("itemName"),
            "price": item.get("itemPrice"),
            "url": item.get("affiliateUrl") or item.get("itemUrl"),
            "image": item.get("mediumImageUrls", [None])[0],
            "source": "Rakuten"
        })

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "rakuten.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": args.keyword, "items": items}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
