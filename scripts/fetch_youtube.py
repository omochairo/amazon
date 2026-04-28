import os
import requests
import json
import sys

def fetch_youtube_reviews(toy_name):
    api_key = os.environ.get("YOUTUBE_API_KEY")
    # 検索キーワードを「おもちゃ名 + レビュー」や「おもちゃ名 + 遊んでみた」に調整
    query = f"{toy_name} おもちゃ レビュー"
    
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "maxResults": 3, # 上位3つを取得
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP"
    }

    response = requests.get(url, params=params)
    data = response.json()
    
    videos = []
    for item in data.get("items", []):
        video_id = item["id"]["videoId"]
        videos.append({
            "title": item["snippet"]["title"],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "embed_url": f"https://www.youtube.com/embed/{video_id}" # 埋め込み用
        })
        
    # 保存処理
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path = os.path.join(base_dir, "data", "youtube_result.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=4)
    print(f"YouTube動画を{len(videos)}件保存しました。")

if __name__ == "__main__":
    toy_name = sys.argv[1] if len(sys.argv) > 1 else "知育玩具"
    fetch_youtube_reviews(toy_name)
