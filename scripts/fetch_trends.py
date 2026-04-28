import requests
import xml.etree.ElementTree as ET
import os

def fetch_google_trends():
    # 日本の急上昇ワードのRSSフィード
    url = "https://trends.google.co.jp/trends/trendingsearches/daily/rss?geo=JP"

    try:
        response = requests.get(url)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        trends = []
        for item in root.findall(".//item"):
            title = item.find("title").text
            approx_traffic = item.find("{https://trends.google.com/trends/trendingsearches/daily}approx_traffic").text
            trends.append(f"{title} (検索数: {approx_traffic})")

        # 保存処理
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "current_trends.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        import json
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(trends, f, ensure_ascii=False, indent=4)

        print("最新のトレンドワードを取得しました。")

    except Exception as e:
        print(f"Google Trends取得エラー: {e}")

if __name__ == "__main__":
    fetch_google_trends()
