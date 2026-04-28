import os
import json
import sys
import re
from amazon_creatorsapi import AmazonCreatorsApi, Country

def validate_url(url):
    return url.startswith("http")

def main(keyword):
    access_key = os.getenv("AMAZON_ACCESS_KEY")
    secret_key = os.getenv("AMAZON_SECRET_KEY")
    # AMAZON_AFFILIATE_TAG を優先し、なければ AMAZON_PARTNER_TAG を使用
    affiliate_tag = os.getenv("AMAZON_AFFILIATE_TAG") or os.getenv("AMAZON_PARTNER_TAG")
    country_code = os.getenv("AMAZON_COUNTRY", "JP")

    if not all([access_key, secret_key, affiliate_tag]):
        print("Error: AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, and AMAZON_AFFILIATE_TAG must be set.")
        sys.exit(1)

    country = getattr(Country, country_code, Country.JP)

    api = AmazonCreatorsApi(
        credential_id=access_key,
        credential_secret=secret_key,
        tag=affiliate_tag,
        country=country
    )

    print(f"--- Amazon Fetch Log ---")
    print(f"Input Keyword: {keyword}")
    print(f"Target Country: {country_code}")
    
    try:
        search_result = api.search_items(keywords=keyword)

        items = []
        for item in search_result.items:
            asin = item.asin
            # リンク構造の強制: https://www.amazon.co.jp/dp/[ASIN]/?tag=[TAG]
            affiliate_url = f"https://www.amazon.co.jp/dp/{asin}/?tag={affiliate_tag}"

            # 自己検閲
            if not validate_url(affiliate_url):
                print(f"Warning: Invalid URL generated for ASIN {asin}")
                continue

            title = item.item_info.title.display_value if item.item_info and item.item_info.title else "No Title"
            price = item.offers.listings[0].price.display_amount if item.offers and item.offers.listings and item.offers.listings[0].price else "N/A"

            print(f"Product Name: {title}")
            print(f"Final Affiliate URL: {affiliate_url}")

            item_data = {
                "asin": asin,
                "title": title,
                "price": price,
                "url": affiliate_url,
                "source": "Amazon"
            }
            items.append(item_data)

        results = {
            "keyword": keyword,
            "items": items
        }

        os.makedirs("data", exist_ok=True)
        # 互換性のために両方のファイル名で保存
        with open("data/amazon_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)
        with open("data/search_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(items)} items to data/amazon_result.json and data/search_result.json")

    except Exception as e:
        print(f"An error occurred during Amazon API call: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_amazon.py <keyword>")
