import os
import json
import sys
from amazon_creatorsapi import AmazonCreatorsApi, Country

def main(keyword):
    access_key = os.getenv("AMAZON_ACCESS_KEY")
    secret_key = os.getenv("AMAZON_SECRET_KEY")
    partner_tag = os.getenv("AMAZON_PARTNER_TAG")
    country_code = os.getenv("AMAZON_COUNTRY", "JP")

    if not all([access_key, secret_key, partner_tag]):
        print("Error: AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, and AMAZON_PARTNER_TAG must be set.")
        sys.exit(1)

    country = getattr(Country, country_code, Country.JP)

    api = AmazonCreatorsApi(
        credential_id=access_key,
        credential_secret=secret_key,
        tag=partner_tag,
        country=country
    )

    print(f"Searching for: {keyword} in {country_code}")
    
    try:
        search_result = api.search_items(keywords=keyword)

        items = []
        for item in search_result.items:
            # 必要な情報を抽出
            item_data = {
                "asin": item.asin,
                "title": item.item_info.title.display_value if item.item_info and item.item_info.title else "No Title",
                "price": item.offers.listings[0].price.display_amount if item.offers and item.offers.listings and item.offers.listings[0].price else "N/A",
                "url": item.detail_page_url
            }
            items.append(item_data)

        results = {
            "keyword": keyword,
            "items": items
        }

        with open("data/search_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(items)} items to data/search_result.json")

    except Exception as e:
        print(f"An error occurred during API call: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_amazon.py <keyword>")
