import json
import os
import re
from datetime import datetime

<<<<<<< HEAD
def load_json(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def main():
    data_dir = "data"
    keyword = "Product"

    # 商品データ
    amazon_data = load_json(os.path.join(data_dir, "amazon_result.json"))
    rakuten_data = load_json(os.path.join(data_dir, "rakuten_result.json"))
    yahoo_data = load_json(os.path.join(data_dir, "yahoo_result.json"))

    if amazon_data: keyword = amazon_data.get("keyword", keyword)

    # その他データ
    youtube_data = load_json(os.path.join(data_dir, "youtube_result.json"))
    news_data = load_json(os.path.join(data_dir, "news_result.json"))
    trends_data = load_json(os.path.join(data_dir, "trends_result.json"))
    internal_links_data = load_json(os.path.join(data_dir, "internal_links_result.json"))

    # 記事の内容を作成
    title = f"おすすめの{keyword}徹底比較・最新情報まとめ"
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

=======
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

>>>>>>> origin/main
    content = f"""---
title: "{title}"
date: {date}
draft: false
---

<<<<<<< HEAD
人気の「{keyword}」について、商品比較から最新ニュース、役立つ動画まで情報をまとめました。

## 商品比較一覧

"""

    # 商品リストの結合
    all_products = []
    if amazon_data: all_products.extend(amazon_data.get("items", []))
    if rakuten_data: all_products.extend(rakuten_data.get("items", []))
    if yahoo_data: all_products.extend(yahoo_data.get("items", []))

    for item in all_products:
        source = item.get("source", "Unknown")
        title_text = item['title']
        if not title_text.startswith(f"[{source}]"):
            display_title = f"[{source}] {title_text}"
        else:
            display_title = title_text

        content += f"""### {display_title}
- **価格**: {item['price']}
- **詳細・購入はこちら**: [{item['title']}]({item['url']})
- **情報元**: {source}
=======
Amazonで人気の「{keyword}」をいくつかピックアップしてご紹介します。

## 商品一覧

"""

    for item in items:
        content += f"""### {item['title']}
- **価格**: {item['price']}
- **詳細はこちら**: [{item['title']}]({item['url']})
- **ASIN**: {item['asin']}
>>>>>>> origin/main

---
"""

<<<<<<< HEAD
    # トレンド情報
    if trends_data and trends_data.get("top_queries"):
        content += f"\n## 「{keyword}」に関するトレンドワード\n\n"
        for query in trends_data["top_queries"]:
            content += f"- {query['query']}\n"
        content += "\n"

    # YouTube動画
    if youtube_data and youtube_data.get("items"):
        content += f"\n## 関連動画\n\n"
        for video in youtube_data["items"]:
            content += f"### {video['title']}\n"
            content += f"[![{video['title']}]({video['thumbnail']})]({video['url']})\n\n"
        content += "\n"

    # 最新ニュース
    if news_data and news_data.get("items"):
        content += f"\n## 最新ニュース\n\n"
        for news in news_data["items"]:
            content += f"- [{news['title']}]({news['url']}) ({news['published']})\n"
        content += "\n"

    # 内部リンク
    if internal_links_data and internal_links_data.get("items"):
        content += f"\n## 関連記事\n\n"
        for link in internal_links_data["items"]:
            content += f"- [{link['title']}]({link['url']})\n"
        content += "\n"

    # ファイル名を作成
=======
    # ファイル名を作成
    # Hugoは日本語のファイル名も扱えるが、URLセーフな名前にすることを検討
    # ここではシンプルに、ASINなどをベースにするか、キーワードをそのまま使う（ユーザーの元々の要望に合わせる）
    # ただし、記号などは除去する
>>>>>>> origin/main
    safe_keyword = re.sub(r'[\\/*?:"<>|]', "", keyword)
    filename = f"{safe_keyword}.md"
    filepath = os.path.join("content/posts", filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Article generated: {filepath}")

if __name__ == "__main__":
    main()
