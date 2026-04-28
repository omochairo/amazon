import requests
import xml.etree.ElementTree as ET
import os
import json

def fetch_yahoo_news():
    # 育児やトレンドに近い「ライフ」や「IT・科学」のRSS
    urls = {
        "life": "https://news.yahoo.co.jp/rss/categories/life.xml",
        "it": "https://news.yahoo.co.jp/rss/categories/it.xml"
    }

    news_list = []

    for cat, url in urls.items():
        try:
            response = requests.get(url)
            root = ET.fromstring(response.content)

            for item in root.findall(".//item")[:5]: # 各カテゴリ上位5件
                title = item.find("title").text
                link = item.find("link").text
                pub_date = item.find("pubDate").text
                news_list.append({
                    "category": cat,
                    "title": title,
                    "link": link,
                    "date": pub_date
                })
        except Exception as e:
            print(f"Yahooニュース取得エラー ({cat}): {e}")

    # 保存
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "yahoo_news.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(news_list, f, ensure_ascii=False, indent=4)
    print(f"Yahooニュースを{len(news_list)}件保存しました。")

if __name__ == "__main__":
    fetch_yahoo_news()
