import requests
import os
import json
import logging
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("internal_links")

def get_related_articles(keyword):
    # APIキー不要のスクレイピング形式に変更するか、
    # おもちゃいろのサイト内検索などキー不要な方法を模索
    # ここでは公開されているRSSや検索ページを簡易的に利用する例
    search_url = f"https://omcha.jp/?s={keyword}"
    links = []
    try:
        resp = requests.get(search_url, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # サイトの構造に合わせて調整 (PaperModやWordPressの標準的な構造)
        for article in soup.select('article')[:3]:
            title_tag = article.select_one('h2.entry-title a, h2 a')
            if title_tag:
                links.append({
                    "title": title_tag.get_text(strip=True),
                    "url": title_tag.get('href')
                })
    except Exception as e:
        logger.error(f"Error scraping internal links: {e}")

    return links

if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "知育玩具"
    res = get_related_articles(kw)
    print(json.dumps(res, ensure_ascii=False, indent=2))
