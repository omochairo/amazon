import os
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
