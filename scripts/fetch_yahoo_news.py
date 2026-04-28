<<<<<<< HEAD
import os
import json
import sys
import feedparser
import urllib.parse

def main(keyword):
    print(f"--- Yahoo News Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    try:
        # Yahoo News RSS (Search)
        encoded_keyword = urllib.parse.quote(keyword)
        rss_url = f"https://news.yahoo.co.jp/rss/search?p={encoded_keyword}"

        feed = feedparser.parse(rss_url)

        news_items = []
        for entry in feed.entries[:5]:
            title = entry.title
            url = entry.link

            print(f"News Title: {title}")
            print(f"News URL: {url}")

            news_items.append({
                "title": title,
                "url": url,
                "published": entry.published if hasattr(entry, 'published') else ""
            })

        results = {
            "keyword": keyword,
            "items": news_items
        }

        os.makedirs("data", exist_ok=True)
        with open("data/news_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(news_items)} news to data/news_result.json")

    except Exception as e:
        print(f"An error occurred during Yahoo News Fetch: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_yahoo_news.py <keyword>")
=======
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
>>>>>>> origin/main
