import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
import re

q = urllib.parse.quote("Mamimami Home 積み木 プルトイ")
url = f"https://html.duckduckgo.com/html/?q={q}"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
try:
    html = urllib.request.urlopen(req).read().decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", class_="result__snippet")[:10]:
        print(a.text)
        href = a.get("href")
        if href and href.startswith("//duckduckgo.com/l/?uddg="):
            real_url = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            print("Real URL:", real_url)
except Exception as e:
    print("Error:", e)
