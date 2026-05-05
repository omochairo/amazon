import os
import requests
import json
import sys
import logging

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fetch_books")

def fetch_books(keyword):
    api_key = os.environ.get("GOOGLEBOOKS_API_KEY")
    if not api_key:
        logger.warning("GOOGLEBOOKS_API_KEY missing. Skipping Google Books fetch.")
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "books_result.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump({"keyword": keyword, "items": []}, f, ensure_ascii=False, indent=4)
        return

    # 知育・育児ブログ向けに検索クエリを最適化
    query = f"{keyword} 知育 絵本"

    url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": query,
        "key": api_key,
        "maxResults": 3,
        "langRestrict": "ja",
        "orderBy": "relevance"
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"Google Books APIエラー: {e}")
        sys.exit(1)

    items = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        image_links = info.get("imageLinks", {})
        thumbnail = image_links.get("thumbnail") or image_links.get("smallThumbnail")

        # Use https for images
        if thumbnail and thumbnail.startswith("http://"):
            thumbnail = thumbnail.replace("http://", "https://")

        items.append({
            "title": info.get("title"),
            "authors": info.get("authors", ["不明"]),
            "description": info.get("description", "説明なし"),
            "url": info.get("infoLink"),
            "image": thumbnail
        })

    result = {
        "keyword": keyword,
        "items": items
    }

    # 保存処理
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "books_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

    logger.info(f"関連書籍を {len(items)} 件取得し、{save_path} に保存しました。")

if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "知育"
    fetch_books(keyword)
