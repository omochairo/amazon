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

    search_kw = args.keyword if args.keyword else "知育玩具"

    # Attempt 1: Standard Affiliate API
    url_aff = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params_aff = {
        "applicationId": app_id,
        "keyword": search_kw,
        "formatVersion": 2,
        "hits": 10
    }
    if aff_id: params_aff["affiliateId"] = aff_id

    # Attempt 2: RMS API (2026 update)
    url_rms = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    params_rms = params_aff.copy()
    if access_key:
        params_rms["accessKey"] = access_key

    headers = {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon"
    }

    # Try RMS first if access_key is present, otherwise Affiliate API
    url = url_rms if access_key else url_aff
    params = params_rms if access_key else params_aff

    resp = requests.get(url, params=params, headers=headers)

    if resp.status_code != 200:
        # Fallback to the other API
        logger.warning(f"Rakuten first attempt failed ({url}): {resp.text}. Trying fallback API...")
        url = url_aff if url == url_rms else url_rms
        params = params_aff if url == url_aff else params_rms
        resp = requests.get(url, params=params, headers=headers)

        if resp.status_code != 200:
            logger.error(f"Rakuten fallback error ({url}): {resp.text}")
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
