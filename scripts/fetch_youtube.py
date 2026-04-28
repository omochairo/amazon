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
logger = logging.getLogger("fetch_youtube")

def fetch_youtube_reviews(keyword):
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        logger.error("YOUTUBE_API_KEY が設定されていません。")
        return

    # 検索キーワードを「商品名 + レビュー」や「商品名 + 知育」に調整
    search_query = f"{keyword} おもちゃ レビュー"

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key,
        "q": search_query,
        "part": "snippet",
        "maxResults": 5, # 少し多めに取得
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP",
        "order": "relevance"
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"YouTube APIエラー: {e}")
        return

    items = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue

        snippet = item.get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})
        # 高画質版を優先
        thumbnail_url = (
            thumbnails.get("high", {}).get("url") or
            thumbnails.get("medium", {}).get("url") or
            thumbnails.get("default", {}).get("url")
        )

        items.append({
            "title": snippet.get("title"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": thumbnail_url,
            "channel": snippet.get("channelTitle"),
            "publishedAt": snippet.get("publishedAt")
        })

    result = {
        "keyword": keyword,
        "items": items
    }

    # 保存処理
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "youtube_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

    logger.info(f"YouTube動画を {len(items)} 件取得し、{save_path} に保存しました。")

if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "知育玩具"
    fetch_youtube_reviews(keyword)
