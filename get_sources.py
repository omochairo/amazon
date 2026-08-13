import urllib.request
from bs4 import BeautifulSoup
import re

urls = [
    "https://kakaku.com/item/S0001004124/",
    "https://www.yodobashi.com/product/100000001008594772/",
    "https://www.lego.com/ja-jp/product/adventures-with-interactive-lego-peach-71441"
]

for url in urls:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req).read().decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        print(f"URL: {url}")
        print(f"Title: {soup.title.string.strip() if soup.title else 'No title'}")

        # Get some text to use as notes
        text = soup.get_text()
        text = re.sub(r'\s+', ' ', text)
        print(f"Text snippet: {text[:500]}")
        print("-" * 40)
    except Exception as e:
        print(f"Failed for {url}: {e}")
