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
