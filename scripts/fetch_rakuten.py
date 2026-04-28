import os
<<<<<<< HEAD
import json
import sys
from rakuten_ws import RakutenWebService

def validate_url(url):
    return url.startswith("http")

def main(keyword):
    app_id = os.getenv("RAKUTEN_APPLICATION_ID")
    affiliate_id = os.getenv("RAKUTEN_AFFILIATE_ID")

    if not app_id:
        print("Error: RAKUTEN_APPLICATION_ID must be set.")
        sys.exit(1)

    ws = RakutenWebService(application_id=app_id, affiliate_id=affiliate_id)

    print(f"--- Rakuten Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    try:
        # 楽天市場商品検索API
        search_result = ws.ichiba.item.search(keyword=keyword)

        items = []
        for item in search_result:
            title = item['itemName']
            price = item['itemPrice']

            # アフィリエイトURLの取得または生成
            affiliate_url = item.get('affiliateUrl')
            if not affiliate_url:
                # バックアップロジック: itemUrl に手動でアフィリエイトIDを付与
                item_url = item['itemUrl']
                if affiliate_id:
                    affiliate_url = f"https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?pc={item_url}&m={item_url}"
                else:
                    affiliate_url = item_url

            # 自己検閲
            if not validate_url(affiliate_url):
                print(f"Warning: Invalid URL generated for item {title}")
                continue

            print(f"Product Name: {title}")
            print(f"Final Affiliate URL: {affiliate_url}")

            item_data = {
                "id": item['itemCode'],
                "title": title,
                "price": f"￥{price}",
                "url": affiliate_url,
                "source": "Rakuten"
            }
            items.append(item_data)

        results = {
            "keyword": keyword,
            "items": items
        }

        os.makedirs("data", exist_ok=True)
        with open("data/rakuten_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(items)} items to data/rakuten_result.json")

    except Exception as e:
        print(f"An error occurred during Rakuten API call: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_rakuten.py <keyword>")
=======
import requests
import json
import sys

def fetch_rakuten(keyword):
    # 2026年からの新エンドポイント
    url = "https://openapi.rakuten.co.jp/ichibams/api/202602/item/search"

    params = {
        "applicationId": os.environ.get("RAKUTEN_APP_ID"),
        "accessKey": os.environ.get("RAKUTEN_ACCESS_KEY"),
        "affiliateId": os.environ.get("RAKUTEN_AFFILIATE_ID"),
        "keyword": keyword,
        "format": "json",
        "hits": 5
    }

    # 2026年仕様：Refererヘッダーが必須になるケースがあります
    headers = {
        "Referer": "https://omochairo.github.io/amazon/"
    }

    response = requests.get(url, params=params, headers=headers)
    data = response.json()

    # 抽出して保存
    items = []
    for i in data.get("Items", []):
        item = i.get("Item", {})
        items.append({
            "title": item.get("itemName"),
            "price": item.get("itemPrice"),
            "url": item.get("affiliateUrl"),
            "shop": "Rakuten"
        })

    os.makedirs("data", exist_ok=True)
    with open("data/rakuten_result.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        fetch_rakuten(sys.argv[1])
>>>>>>> origin/main
