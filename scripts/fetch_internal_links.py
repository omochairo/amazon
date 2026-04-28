import os
import requests
import json
import sys

def fetch_related_articles(keyword):
    # WordPressサイトのURL（末尾にスラッシュなし）
    wp_url = "https://omcha.jp"
    api_key = os.environ.get("IRO_API_KEY") # GitHub Secretsに登録

    url = f"{wp_url}/wp-json/iro/v1/related"
    params = {
        "keyword": keyword,
        "count": 5,
        "api_key": api_key
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        # APIが返してくれる markdown_links をそのまま保存
        md_links = data.get("markdown_links", [])

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_path = os.path.join(base_dir, "data", "internal_links.json")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(md_links, f, ensure_ascii=False, indent=4)
        print(f"関連記事を{len(md_links)}件取得しました。")

    except Exception as e:
        print(f"関連記事APIエラー: {e}")

if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "知育玩具"
    fetch_related_articles(keyword)
