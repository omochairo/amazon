import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
import re

def search_yahoo(query):
    encoded_query = urllib.parse.quote(query)
    url = f"https://search.yahoo.co.jp/search?p={encoded_query}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    )
    try:
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore')
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        for h3 in soup.find_all('h3', class_='sw-Card__title'):
            a = h3.find('a')
            if a and a.get('href'):
                results.append(a.get('href'))
        return results[:5]
    except Exception as e:
        print(f"Error: {e}")
        return []

print(search_yahoo("アンパンマン うちの子天才 トランペット STマーク"))
