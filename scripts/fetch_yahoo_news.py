import requests
import xml.etree.ElementTree as ET
import os
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_yahoo_news")

def fetch_yahoo_news():
    # 育児やトレンドに近い「ライフ」のRSS
    urls = {
        "life": "https://news.yahoo.co.jp/rss/categories/life.xml",
    }

    news_list = []

    for cat, url in urls.items():
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            root = ET.fromstring(response.content)

            for item in root.findall(".//item")[:10]: # 上位10件
                title = item.find("title").text
                link = item.find("link").text
                pub_date = item.find("pubDate").text
                news_list.append({
                    "category": cat,
                    "title": title,
                    "url": link,
                    "published": pub_date
                })
        except Exception as e:
            logger.error(f"Yahooニュース取得エラー ({cat}): {e}")

    result = {
        "items": news_list
    }

    # プロジェクトルートのdataフォルダに保存
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "news_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    logger.info(f"Yahooニュースを {len(news_list)} 件取得し、{save_path} に保存しました。")

if __name__ == "__main__":
    fetch_yahoo_news()
