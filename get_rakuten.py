import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
import json

query = urllib.parse.quote("もりもり工房 トミカ 収納")
url = f"https://search.rakuten.co.jp/search/mall/{query}/"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
try:
    html = urllib.request.urlopen(req).read().decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".searchresultitem")
    if items:
        for item in items[:2]:
            a = item.select_one("a")
            if a:
                print(a.get("href"))
except Exception as e:
    print(e)
