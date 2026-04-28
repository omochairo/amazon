import json
import os
import re
from datetime import datetime

def main():
    data_dir = "data"
    all_items = []
    keyword = "Product"

    # 指定されたJSONファイルを読み込む
    files = ["amazon_result.json", "rakuten_result.json", "yahoo_result.json"]

    for filename in files:
        filepath = os.path.join(data_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                keyword = data.get("keyword", keyword)
                all_items.extend(data.get("items", []))

    if not all_items:
        print("No items found in any JSON file.")
        return

    # 記事の内容を作成
    title = f"おすすめの{keyword}比較紹介"
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

    content = f"""---
title: "{title}"
date: {date}
draft: false
---

人気の「{keyword}」を、Amazon・楽天・Yahoo!ショッピングからピックアップしてご紹介します。

## 商品比較一覧

"""

    for item in all_items:
        source = item.get("source", "Unknown")
        title_text = item['title']
        # 既にソース名が含まれている場合は重複を避ける
        if title_text.startswith(f"[{source}]"):
            display_title = title_text
        else:
            display_title = f"[{source}] {title_text}"

        content += f"""### {display_title}
- **価格**: {item['price']}
- **詳細・購入はこちら**: [{item['title']}]({item['url']})
- **情報元**: {source}

---
"""

    # ファイル名を作成
    safe_keyword = re.sub(r'[\\/*?:"<>|]', "", keyword)
    filename = f"{safe_keyword}.md"
    filepath = os.path.join("content/posts", filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Article generated: {filepath}")

if __name__ == "__main__":
    main()
