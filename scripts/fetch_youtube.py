import os, sys, json, requests, logging

def get_secret(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"[FATAL] {name} not set.\n")
        sys.exit(2)
    return v

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_youtube")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    api_key = get_secret("YOUTUBE_API_KEY")
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key,
        "q": f"{args.keyword} おもちゃ レビュー",
        "part": "snippet",
        "maxResults": 5,
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP"
    }

    resp = requests.get(url, params=params)
    data = resp.json()
    items = []
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
