import os
import json
import sys
# 本来はここにAmazon公式のSDK（python-amazon-paapi等）の読み込みが入ります
# Julesがこれを実行して、結果をJSONで保存するように指示します

def main(keyword):
    # ここにAmazon APIを叩く処理を書く（Julesに完成させてもらうのもアリです）
    print(f"Searching for: {keyword}")
    
    # テスト用のダミーデータ
    results = {
        "keyword": keyword,
        "items": [
            {"asin": "B0XXXXXX", "title": "サンプル商品", "price": 5000}
        ]
    }
    
    with open("data/search_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
