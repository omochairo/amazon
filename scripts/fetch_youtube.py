import os, sys, json, requests, logging

def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_youtube")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    api_key = get_secret("YOUTUBE_API_KEY")
    items = []

    if not api_key:
        logger.warning("YouTube API keys missing. Skipping YouTube fetch (returning empty data).")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False, indent=4)
        return

    url = "https://www.googleapis.com/youtube/v3/search"
    search_kw = args.keyword if args.keyword else "知育玩具"
    params = {
        "key": api_key,
        "q": f"{search_kw} おもちゃ レビュー",
        "part": "snippet",
        "maxResults": 5,
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP"
    }

    resp = requests.get(url, params=params)
    data = resp.json()
    for item in data.get("items", []):
        video_id = item["id"]["videoId"]
        items.append({
            "title": item["snippet"]["title"],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": item["snippet"]["thumbnails"]["high"]["url"]
        })

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
