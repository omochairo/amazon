"""
fetch_youtube.py
ジャンル全体 (--keyword) 検索に加え、data/raw/amazon.json の各 ASIN について
brand+model 番号での個別検索も実行し、結果をマージして data/raw/youtube.json に保存する。

Phase 2 (3a) で追加した filter_raw_per_asin.py が参照するプールに、
ASIN と関連性の高い動画が含まれるようにするための改修。
"""

import os
import sys
import json
import re
import logging
import pathlib
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_youtube")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# 既知ブランド (filter_raw_per_asin.py と一致させること)
KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "ボーネルンド",
    "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー", "セガトイズ",
    "エポック", "アガツマ", "ジョイレア",
]


def get_secret(name: str) -> str:
    return os.environ.get(name)


def extract_model_number(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4})\b", text)
    if m:
        return m.group(1)
    return ""


def extract_brand(text: str) -> str:
    for b in KNOWN_BRANDS:
        if b in text:
            return b
    return ""


def build_per_asin_query(title: str) -> str:
    """ASIN タイトルから YouTube 検索向けクエリ文字列を作る。
    優先度: brand+モデル番号 > brand+先頭2-3トークン > タイトル先頭30字"""
    brand = extract_brand(title)
    model = extract_model_number(title)
    if brand and model:
        return f"{brand} {model}"
    if brand:
        # ブランド + シリーズらしい部分 (タイトルの先頭から2語)
        clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title).replace(brand, "")
        tokens = [t for t in clean.split() if len(t) >= 2][:2]
        return f"{brand} {' '.join(tokens)}".strip()
    return title[:30]


def youtube_search(api_key: str, query: str, max_results: int = 5) -> list:
    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "maxResults": max_results,
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP",
    }
    try:
        resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning(
                f"YouTube search failed for '{query}': HTTP {resp.status_code} body={resp.text[:300]}"
            )
            return []
        data = resp.json()
        out = []
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            sn = item.get("snippet", {})
            out.append({
                "title": sn.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": sn.get("thumbnails", {}).get("high", {}).get("url", ""),
            })
        return out
    except Exception as e:
        logger.error(f"YouTube search error for '{query}': {e}")
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    api_key = get_secret("YOUTUBE_API_KEY")
    items = []

    if not api_key:
        logger.warning("YouTube API key missing. Skipping fetch.")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False, indent=4)
        return

    seen_urls = set()

    # 1. ジャンル全体検索
    genre_kw = args.keyword if args.keyword else "知育玩具"
    logger.info(f"Genre search: {genre_kw}")
    for v in youtube_search(api_key, f"{genre_kw} おもちゃ レビュー", max_results=5):
        if v["url"] not in seen_urls:
            items.append(v)
            seen_urls.add(v["url"])

    # 2. ASIN 別検索 (per_asin/ フィルタが意味あるコンテンツを得られるように)
    amazon_path = pathlib.Path(args.out) / "amazon.json"
    if amazon_path.exists():
        try:
            amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
            asin_items = amazon.get("items", [])
            logger.info(f"Per-ASIN search: {len(asin_items)} ASINs")
            for amz in asin_items:
                title = amz.get("title", "")
                if not title:
                    continue
                query = build_per_asin_query(title)
                if not query:
                    continue
                logger.info(f"  [{amz.get('asin')}] query='{query}'")
                for v in youtube_search(api_key, query, max_results=3):
                    if v["url"] not in seen_urls:
                        items.append(v)
                        seen_urls.add(v["url"])
        except Exception as e:
            logger.warning(f"Per-ASIN YouTube search skipped: {e}")
    else:
        logger.info("amazon.json not found, skipping per-ASIN search")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(items)} videos to youtube.json")


if __name__ == "__main__":
    main()
