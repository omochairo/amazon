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

import _fetch_targets

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
    import argparse
    parser = argparse.ArgumentParser()
    # sys.argv[1] 互換のための positional (旧呼出 `fetch_books.py "<keyword>"`)
    parser.add_argument("keyword", nargs="?", default="知育")
    parser.add_argument("--out", default=None,
                        help="出力先 (省略時は repo root/data/raw/)")
    parser.add_argument("--articles-dir", default=None,
                        help="article ASIN を per-ASIN target に union するソース")
    parser.add_argument("--max-per-run", type=int, default=50,
                        help="1 run あたりの per-ASIN query 上限")
    parser.add_argument("--stale-after-days", type=int, default=7,
                        help="この日数以内に query 済の ASIN はスキップ")
    args = parser.parse_args()
    keyword = args.keyword or "知育"

    api_key = os.environ.get("GOOGLEBOOKS_API_KEY")
    base_dir = pathlib.Path(__file__).resolve().parent.parent
    out_dir = pathlib.Path(args.out) if args.out else (base_dir / "data" / "raw")
    articles_dir = pathlib.Path(args.articles_dir) if args.articles_dir else (base_dir / "data" / "articles")
    save_path = out_dir / "books_result.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not api_key:
        logger.warning("GOOGLEBOOKS_API_KEY missing. Skipping fetch.")
        save_path.write_text(json.dumps({"keyword": keyword, "items": []}, ensure_ascii=False, indent=4),
                             encoding="utf-8")
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

    # 2. ASIN 別検索 (stale-first 巡回)
    amazon_items = []
    amazon_path = out_dir / "amazon.json"
    if amazon_path.exists():
        try:
            amazon_items = json.loads(amazon_path.read_text(encoding="utf-8")).get("items", [])
        except json.JSONDecodeError:
            logger.warning("amazon.json unreadable; using article targets only")

    targets = _fetch_targets.pick_target_asins(
        out_dir=out_dir,
        source="books",
        amazon_items=amazon_items,
        articles_dir=articles_dir,
        max_per_run=args.max_per_run,
        stale_after_days=args.stale_after_days,
    )

    # ブランド共通の query になりやすいので same-query dedup (API 節約)
    seen_queries: set[str] = set()
    queried_asins: list[str] = []
    for asin, title in targets:
        brand = extract_brand(title)
        if brand:
            query = f"{brand} 知育"
        else:
            fb = extract_fallback_keyword(title)
            if not fb:
                continue
            query = f"{fb} 知育"
        per_asin_items: list = []
        if query not in seen_queries:
            seen_queries.add(query)
            logger.info(f"  [{asin}] brand='{brand or '-'}' query='{query}'")
            for b in books_search(api_key, query, max_results=2):
                per_asin_items.append(b)
                url = b.get("url") or ""
                if url and url not in seen_urls:
                    items.append(b)
                    seen_urls.add(url)
        else:
            logger.info(f"  [{asin}] query='{query}' (deduped, no API call)")
        _fetch_targets.write_per_asin_raw(out_dir, "books", asin, query, per_asin_items)
        queried_asins.append(asin)

    if queried_asins:
        _fetch_targets.mark_queried(out_dir, "books", queried_asins)

    save_path.write_text(json.dumps({"keyword": keyword, "items": items}, ensure_ascii=False, indent=4),
                         encoding="utf-8")
    logger.info(f"Saved {len(items)} books to {save_path} (queried {len(queried_asins)} ASINs)")


if __name__ == "__main__":
    main()
