import json
import os
import re
from datetime import datetime

def main():
    json_path = "data/search_result.json"
    if not os.path.exists(json_path):
        print(f"Error: {json_path} not found.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keyword = data.get("keyword", "Amazon Product")
    items = data.get("items", [])

    if not items:
        print("No items found in JSON.")
        return

    # 記事の内容を作成
    title = f"おすすめの{keyword}比較紹介"
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

    content = f"""---
title: "{title}"
date: {date}
draft: false
---

Amazonで人気の「{keyword}」をいくつかピックアップしてご紹介します。

## 商品一覧

"""

    for item in items:
        content += f"""### {item['title']}
- **価格**: {item['price']}
- **詳細はこちら**: [{item['title']}]({item['url']})
- **ASIN**: {item['asin']}

---
"""

    # ファイル名を作成
    # Hugoは日本語のファイル名も扱えるが、URLセーフな名前にすることを検討
    # ここではシンプルに、ASINなどをベースにするか、キーワードをそのまま使う（ユーザーの元々の要望に合わせる）
    # ただし、記号などは除去する
    safe_keyword = re.sub(r'[\\/*?:"<>|]', "", keyword)
    filename = f"{safe_keyword}.md"
    filepath = os.path.join("content/posts", filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Article generated: {filepath}")

if __name__ == "__main__":
    main()
