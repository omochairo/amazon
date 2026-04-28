import json
import os
from datetime import datetime

def convert():
    json_path = 'data/search_result.json'
    if not os.path.exists(json_path):
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Hugo用のMarkdown形式を作成
    content = f"""---
title: "{data['keyword']}のおすすめ比較"
date: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S+09:00')}
draft: false
---

## Amazonで見つかった注目の商品
"""
    for item in data['items']:
        content += f"\n### {item['title']}\n- 価格: {item['price']}円\n- [Amazonで見る](https://www.amazon.co.jp/dp/{item['asin']})\n"

    # ファイルに保存
    os.makedirs('content/posts', exist_ok=True)
    with open(f"content/posts/{data['keyword']}.md", "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    convert()
