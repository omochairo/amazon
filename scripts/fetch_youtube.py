import os
import json
import sys
from googleapiclient.discovery import build

def main(keyword):
    api_key = os.getenv("YOUTUBE_API_KEY")

    if not api_key:
        print("Error: YOUTUBE_API_KEY must be set.")
        sys.exit(1)

    print(f"--- YouTube Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    try:
        youtube = build("youtube", "v3", developerKey=api_key)

        search_response = youtube.search().list(
            q=keyword,
            part="id,snippet",
            maxResults=5,
            type="video"
        ).execute()

        videos = []
        for search_result in search_response.get("items", []):
            title = search_result["snippet"]["title"]
            video_id = search_result["id"]["videoId"]
            url = f"https://www.youtube.com/watch?v={video_id}"

            print(f"Video Title: {title}")
            print(f"Video URL: {url}")

            videos.append({
                "title": title,
                "url": url,
                "video_id": video_id,
                "thumbnail": search_result["snippet"]["thumbnails"]["high"]["url"]
            })

        results = {
            "keyword": keyword,
            "items": videos
        }

        os.makedirs("data", exist_ok=True)
        with open("data/youtube_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(videos)} videos to data/youtube_result.json")

    except Exception as e:
        print(f"An error occurred during YouTube API call: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_youtube.py <keyword>")
