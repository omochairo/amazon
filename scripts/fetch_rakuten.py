import os, sys, json, requests, logging

def get_secret(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"[FATAL] {name} not set.\n")
        sys.exit(2)
    return v

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
    aff_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")

    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    headers = {"Authorization": f"Bearer {access_key}"}
    params = {
        "applicationId": app_id,
        "keyword": args.keyword,
        "formatVersion": 2,
        "hits": 10
    }
    if aff_id: params["affiliateId"] = aff_id

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
