import os
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
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        items = []
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
