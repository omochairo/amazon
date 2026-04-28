import os
import json
import sys
import urllib.parse
import requests

def validate_url(url):
    return url.startswith("http")

def main(keyword):
    client_id = os.getenv("YAHOO_CLIENT_ID")
    sid = os.getenv("VALUECOMMERCE_SID")
    pid = os.getenv("VALUECOMMERCE_PID")

    if not client_id:
        print("Error: YAHOO_CLIENT_ID must be set.")
        sys.exit(1)

    print(f"--- Yahoo Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    try:
        # Yahoo!ショッピング商品検索API
        url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
        params = {
            "appid": client_id,
            "query": keyword,
            "results": 10
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        items = []
        for item in data.get('hits', []):
            title = item['name']
            price = item['price']
            item_url = item['url']

            # バリューコマース MyLink形式の生成
            if sid and pid:
                encoded_url = urllib.parse.quote(item_url)
                affiliate_url = f"https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded_url}"
            else:
                affiliate_url = item_url

            # 自己検閲
            if not validate_url(affiliate_url):
                print(f"Warning: Invalid URL generated for item {title}")
                continue

            print(f"Product Name: {title}")
            print(f"Final Affiliate URL: {affiliate_url}")

            item_data = {
                "id": item['code'],
                "title": title,
                "price": f"￥{price}",
                "url": affiliate_url,
                "source": "Yahoo"
            }
            items.append(item_data)

        results = {
            "keyword": keyword,
            "items": items
        }

        os.makedirs("data", exist_ok=True)
        with open("data/yahoo_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(items)} items to data/yahoo_result.json")

    except Exception as e:
        print(f"An error occurred during Yahoo API call: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_yahoo.py <keyword>")
