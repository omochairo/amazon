import os
<<<<<<< HEAD
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
=======
import requests
import json
import sys
import urllib.parse  # URLエンコードに必要

def create_vc_link(target_url):
    """
    Yahoo!の素のURLをバリューコマースのアフィリエイトリンクに変換する
    """
    sid = os.environ.get("VALUECOMMERCE_SID")
    pid = os.environ.get("VALUECOMMERCE_PID")

    if not sid or not pid:
        return target_url # 設定がない場合はそのままのURLを返す

    # URLエンコード（https%3A%2F%2F... という形式にする）
    encoded_url = urllib.parse.quote(target_url)

    # バリューコマースのMyLink基本フォーマット
    vc_link = f"https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded_url}"
    return vc_link

def fetch_yahoo(keyword):
    client_id = os.environ.get("YAHOO_CLIENT_ID")
    url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"

    params = {
        "appid": client_id,
        "query": keyword,
        "results": 5,
        "sort": "-score"
    }

    try:
>>>>>>> origin/main
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        items = []
<<<<<<< HEAD
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
=======
        for hit in data.get("hits", []):
            raw_url = hit.get("url")
            # ここでアフィリエイトリンクに変換！
            affiliate_url = create_vc_link(raw_url)

            items.append({
                "title": hit.get("name"),
                "price": hit.get("price"),
                "url": affiliate_url, # アフィリエイト済みのURLを格納
                "shop": "Yahoo"
            })

        # プロジェクトルートのdataフォルダに保存
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "yahoo_result.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=4)
        print(f"Yahoo!データを保存しました: {save_path}")

    except Exception as e:
        print(f"Yahoo! APIエラー: {e}")

if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "ワイヤレスイヤホン"
    fetch_yahoo(keyword)
>>>>>>> origin/main
