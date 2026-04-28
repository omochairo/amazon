import os
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
