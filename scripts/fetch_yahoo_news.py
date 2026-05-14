"""
fetch_yahoo_news.py
ジャンル横断 + amazon.json の各 ASIN について Google News RSS で関連ニュースを取得し、
data/raw/news.json に items[] フラット形式で保存する。
filter_raw_per_asin.py が後段で ASIN 別にスコアリングして per_asin/<ASIN>/news.json に振り分ける。

旧実装は news.yahoo.co.jp の life カテゴリ RSS のみ取得していたが、グルメ/宿泊系の
知育玩具と無関係な内容で関連度フィルタを通らず per_asin/news.json が全 ASIN 空だった。
"""

import argparse
import json
import logging
import os
import pathlib
import re
import xml.etree.ElementTree as ET

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_yahoo_news")

GNEWS_URL = "https://news.google.com/rss/search"

NOISE = [
    "送料無料", "ポイント10倍", "正規品", "公式", "最新", "予約",
    "おまけ付き", "ラッピング無料", "あす楽", "即納", "税込",
    "知育玩具", "おもちゃ", "プレゼント", "誕生日", "ギフト",
    "2個セット", "3歳から", "男の子", "女の子", "対象年齢",
]

MODEL_PATTERN = re.compile(r"^([A-Z][A-Z0-9\-]{3,11}|\d{4,5})$")


def extract_keyword(title: str) -> str:
    """Amazon タイトルから検索クエリ (ブランド + 型番 or 先頭語) を抽出。"""
    if not title:
        return ""
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    clean = re.sub(r"\(C\).*?(?=\s|$)", "", clean)
    for n in NOISE:
        clean = clean.replace(n, " ")
    tokens = clean.split()
    if not tokens:
        return title[:30]
    models = [t for t in tokens if MODEL_PATTERN.match(t)]
    if models:
        brand = " ".join(tokens[:2])
        return f"{brand} {models[0]}"[:40].strip()
    return " ".join(tokens[:3])[:40].strip()


def gnews_search(query: str, max_results: int = 5) -> list:
    params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    try:
        resp = requests.get(GNEWS_URL, params=params, timeout=15)
        if resp.status_code >= 400:
            logger.warning(f"Google News {resp.status_code} for '{query}'")
            return []
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.warning(f"Google News error for '{query}': {e}")
        return []
    out = []
    for item in root.findall(".//item")[:max_results]:
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        if title_el is None or link_el is None:
            continue
        out.append({
            "title": title_el.text or "",
            "url": link_el.text or "",
            "published": pub_el.text if pub_el is not None else "",
        })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--keyword", default="知育玩具",
                        help="ジャンル全体検索クエリ (フォールバック)")
    args = parser.parse_args()

    items = []
    seen_urls = set()

    logger.info(f"Genre search: {args.keyword}")
    for n in gnews_search(args.keyword, max_results=5):
        url = n.get("url") or ""
        if url and url not in seen_urls:
            items.append(n)
            seen_urls.add(url)

    base_dir = pathlib.Path(__file__).resolve().parent.parent
    amazon_path = base_dir / "data" / "raw" / "amazon.json"
    if amazon_path.exists():
        try:
            amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
            asin_items = amazon.get("items", [])
            logger.info(f"Per-ASIN search: {len(asin_items)} ASINs")
            seen_queries = set()
            for amz in asin_items:
                query = extract_keyword(amz.get("title", ""))
                if not query or query in seen_queries:
                    continue
                seen_queries.add(query)
                logger.info(f"  query='{query}'")
                for n in gnews_search(query, max_results=3):
                    url = n.get("url") or ""
                    if url and url not in seen_urls:
                        items.append(n)
                        seen_urls.add(url)
        except Exception as e:
            logger.warning(f"Per-ASIN news search skipped: {e}")
    else:
        logger.info("amazon.json not found, skipping per-ASIN search")

    os.makedirs(args.out, exist_ok=True)
    save_path = os.path.join(args.out, "news.json")
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(items)} news items to {save_path}")


if __name__ == "__main__":
    main()
