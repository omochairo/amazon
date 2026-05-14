"""
fetch_books.py
ジャンル全体 (--keyword) 検索に加え、data/raw/amazon.json の各 ASIN について
ブランド名での個別検索も実行し、結果をマージして data/raw/books_result.json に保存する。

書籍は商品モデル番号で hit しないため、ブランド名 + "知育絵本" の形式で検索する。
"""

import os
import sys
import json
import re
import logging
import pathlib
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_books")

BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"

KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "ボーネルンド",
    "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー", "セガトイズ",
    "エポック", "アガツマ", "ジョイレア",
]

NOISE = [
    "送料無料", "ポイント10倍", "正規品", "公式", "最新", "予約",
    "おまけ付き", "ラッピング無料", "あす楽", "即納", "税込",
    "知育玩具", "おもちゃ", "プレゼント", "誕生日", "ギフト",
    "2個セット", "3歳から", "男の子", "女の子", "対象年齢",
]


def extract_brand(text: str) -> str:
    for b in KNOWN_BRANDS:
        if b in text:
            return b
    return ""


def extract_fallback_keyword(title: str) -> str:
    """KNOWN_BRANDS にヒットしないタイトルから検索語を抽出。
    括弧/ノイズ語除去後の先頭 2 語を使う。"""
    if not title:
        return ""
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    for n in NOISE:
        clean = clean.replace(n, " ")
    tokens = clean.split()
    if not tokens:
        return ""
    return " ".join(tokens[:2]).strip()


def books_search(api_key: str, query: str, max_results: int = 3) -> list:
    params = {
        "q": query,
        "key": api_key,
        "maxResults": max_results,
        "langRestrict": "ja",
        "orderBy": "relevance",
    }
    try:
        resp = requests.get(BOOKS_URL, params=params, timeout=15)
        if resp.status_code >= 500:
            logger.warning(f"Google Books 5xx for '{query}'")
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Books search error for '{query}': {e}")
        return []

    out = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        thumbnail = info.get("imageLinks", {}).get("thumbnail") \
            or info.get("imageLinks", {}).get("smallThumbnail")
        if thumbnail and thumbnail.startswith("http://"):
            thumbnail = thumbnail.replace("http://", "https://")
        out.append({
            "title": info.get("title"),
            "authors": info.get("authors", ["不明"]),
            "description": info.get("description", "説明なし"),
            "url": info.get("infoLink"),
            "image": thumbnail,
        })
    return out


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "知育"

    api_key = os.environ.get("GOOGLEBOOKS_API_KEY")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "raw", "books_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    if not api_key:
        logger.warning("GOOGLEBOOKS_API_KEY missing. Skipping fetch.")
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump({"keyword": keyword, "items": []}, f, ensure_ascii=False, indent=4)
        return

    items = []
    seen_urls = set()

    # 1. ジャンル全体検索
    genre_query = f"{keyword} 絵本" if "知育" in keyword else f"{keyword} 知育 絵本"
    logger.info(f"Genre search: {genre_query}")
    for b in books_search(api_key, genre_query, max_results=3):
        url = b.get("url") or ""
        if url and url not in seen_urls:
            items.append(b)
            seen_urls.add(url)

    # 2. ASIN 別検索 (ブランド名 + 知育絵本)
    amazon_path = pathlib.Path(base_dir) / "data" / "raw" / "amazon.json"
    if amazon_path.exists():
        try:
            amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
            asin_items = amazon.get("items", [])
            seen_queries = set()
            logger.info(f"Per-ASIN search: {len(asin_items)} ASINs (deduped by query)")
            for amz in asin_items:
                title = amz.get("title", "")
                brand = extract_brand(title)
                if brand:
                    query = f"{brand} 知育"
                else:
                    fb = extract_fallback_keyword(title)
                    if not fb:
                        continue
                    query = f"{fb} 知育"
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                logger.info(f"  brand='{brand or '-'}' query='{query}'")
                for b in books_search(api_key, query, max_results=2):
                    url = b.get("url") or ""
                    if url and url not in seen_urls:
                        items.append(b)
                        seen_urls.add(url)
        except Exception as e:
            logger.warning(f"Per-ASIN books search skipped: {e}")
    else:
        logger.info("amazon.json not found, skipping per-ASIN search")

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump({"keyword": keyword, "items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(items)} books to {save_path}")


if __name__ == "__main__":
    main()
