import os
<<<<<<< HEAD
import json
import sys
import frontmatter

def main(keyword):
    print(f"--- Internal Links Fetch Log ---")
    print(f"Input Keyword: {keyword}")

    posts_dir = "content/posts"
    internal_links = []

    try:
        if os.path.exists(posts_dir):
            for filename in os.listdir(posts_dir):
                if filename.endswith(".md"):
                    filepath = os.path.join(posts_dir, filename)
                    post = frontmatter.load(filepath)

                    title = post.get("title", filename)
                    # キーワードが含まれているか確認（簡易的）
                    if keyword in title or keyword in post.content:
                        url = f"/posts/{filename.replace('.md', '/')}"

                        print(f"Internal Link Title: {title}")
                        print(f"Path: {url}")

                        internal_links.append({
                            "title": title,
                            "url": url
                        })

        results = {
            "keyword": keyword,
            "items": internal_links[:5]
        }

        os.makedirs("data", exist_ok=True)
        with open("data/internal_links_result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

        print(f"Successfully saved {len(internal_links)} internal links to data/internal_links_result.json")

    except Exception as e:
        print(f"An error occurred during Internal Links Fetch: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python scripts/fetch_internal_links.py <keyword>")
=======
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
>>>>>>> origin/main
