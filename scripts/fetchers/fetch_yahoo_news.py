import requests, os, json, xml.etree.ElementTree as ET

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    url = "https://news.yahoo.co.jp/rss/categories/life.xml"
    news = []
    try:
        resp = requests.get(url, timeout=10)
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item")[:10]:
            news.append({
                "title": item.find("title").text,
                "url": item.find("link").text,
                "published": item.find("pubDate").text
            })
    except:
        pass

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "news.json"), "w", encoding="utf-8") as f:
        json.dump({"items": news}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
