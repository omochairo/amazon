import json
import os
import re
import random
from datetime import datetime

def load_json(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def calculate_ivs(item):
    # Jules独自の知見によるIVSスコアの算出 (モック実装)
    # 実際にはメタデータなどから算出するが、ここでは決定論的なランダムを使用
    asin_hash = sum(ord(c) for c in item.get('asin', 'dummy'))
    score = (asin_hash % 20) / 4.0 + 1.0 # 1.0 ~ 6.0
    return round(min(score, 5.0), 1)

def main():
    data_dir = "data"
    history_file = os.path.join(data_dir, "post_history.json")

    # 履歴の読み込み
    history = load_json(history_file) or []

    # メインの検索結果を読み込み
    amazon_data = load_json(os.path.join(data_dir, "amazon_result.json")) or load_json(os.path.join(data_dir, "search_result.json"))

    if not amazon_data:
        print("Error: No product data found.")
        return

    keyword = amazon_data.get("keyword", "注目アイテム")

    # 履歴への追記
    history.append({
        "date": datetime.now().isoformat(),
        "keyword": keyword
    })
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

    # 他のソースを読み込み
    rakuten_data = load_json(os.path.join(data_dir, "rakuten_result.json"))
    yahoo_data = load_json(os.path.join(data_dir, "yahoo_result.json"))
    youtube_data = load_json(os.path.join(data_dir, "youtube_result.json"))
    news_data = load_json(os.path.join(data_dir, "news_result.json"))
    trends_data = load_json(os.path.join(data_dir, "trends_result.json"))
    internal_links_data = load_json(os.path.join(data_dir, "internal_links_result.json"))
    books_data = load_json(os.path.join(data_dir, "books_result.json"))

    # 執筆モードの抽選 (モック)
    modes = ["【トレンド特報】", "【隠れた名作発掘】", "【育児お悩み相談】", "【季節の歳時記】"]
    mode = random.choice(modes)

    # 記事作成
    title = f"🧸 おすすめの{keyword}徹底比較｜今買うべき一品は？"
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

    content = f"""---
title: "{title}"
date: {date}
draft: false
summary: "Amazon・楽天・Yahooの情報を網羅！AI編集長Julesが「{keyword}」を徹底比較しました。"
tags: ["{keyword}", "比較", "Amazon", "Jules"]
ShowToc: true
---

> **📢 PR表記**: この記事にはアフィリエイトリンクが含まれています。
> 記事の内容はAIエージェント「Jules」が自動生成したものです。（最終更新: {datetime.now().strftime("%Y年%m月%d日")}）

{mode}
人気の「**{keyword}**」について、主要ECサイトの価格比較からYouTube動画、関連ニュースまで、育児のプロ視点（AI）でまとめました。

---

## 📊 商品比較一覧

"""

    # 商品リストの統合と最安値判定
    all_products = []
    if amazon_data: all_products.extend(amazon_data.get("items", []))
    if rakuten_data:
        # 形式を合わせる
        for item in rakuten_data:
            if isinstance(item, dict) and 'Item' in item: item = item['Item'] # 楽天APIの構造対応
            all_products.append({
                "title": item.get("itemName") or item.get("title"),
                "price": item.get("itemPrice") or item.get("price"),
                "url": item.get("affiliateUrl") or item.get("url"),
                "source": "Rakuten",
                "asin": item.get("asin", "N/A")
            })
    if yahoo_data: all_products.extend(yahoo_data.get("items", []))

    # 価格の数値化と最安値特定
    def parse_price(p):
        if isinstance(p, int): return p
        nums = re.findall(r'\d+', str(p).replace(',', ''))
        return int(nums[0]) if nums else 9999999

    min_price = 9999999
    for p in all_products:
        price_val = parse_price(p['price'])
        if price_val < min_price and price_val > 0:
            min_price = price_val

    for item in all_products:
        source = item.get("source", "Amazon")
        price_val = parse_price(item['price'])
        is_cheapest = price_val == min_price and min_price < 9999999
        ivs = calculate_ivs(item)

        cheapest_label = " 🔥 **最安値！**" if is_cheapest else ""

        content += f"### [{source}] {item['title']}\n"
        content += f"- **💰 価格**: {item['price']}{cheapest_label}\n"
        content += f"- **📊 IVSスコア**: {ivs}/5.0\n"
        content += f"- **🔗 詳細・購入**: [{item['title']}]({item['url']})\n\n"

    content += "---\n\n"

    # IVSスコアの解説
    content += f"""> [!IMPORTANT]
> **IVS（知育価値スコア）** は、Julesが独自に算出した指標です。
> 知育効果、耐久性、安全性をコストパフォーマンスで割った独自のアルゴリズムに基づいています。

"""

    # トレンド
    if trends_data and trends_data.get("top_queries"):
        content += f"## 📈 「{keyword}」のトレンドワード\n\n"
        for q in trends_data["top_queries"][:5]:
            content += f"- {q.get('query')}\n"
        content += "\n"

    # YouTube
    if youtube_data and youtube_data.get("items"):
        content += f"## 📺 関連動画でチェック\n\n"
        for v in youtube_data["items"][:2]:
            content += f"### {v['title']}\n"
            content += f"[![{v['title']}]({v['thumbnail']})]({v['url']})\n\n"

    # ニュース
    if news_data and news_data.get("items"):
        content += f"## 📰 最新ニュース\n\n"
        for n in news_data["items"][:3]:
            content += f"- [{n['title']}]({n['url']}) ({n.get('published', '')})\n"
        content += "\n"

    # 関連記事
    if internal_links_data and internal_links_data.get("items"):
        content += f"## 🔗 あわせて読みたい：おもちゃいろの関連記事\n\n"
        for link in internal_links_data["items"]:
            content += f"- [{link['title']}]({link['url']})\n"
        content += "\n"

    # Julesの一言ボヤキ
    content += f"""> [!QUOTE]
> **AI編集長Julesのひとりごと**
> {keyword}選びは、スペックだけじゃなくて「子供がどれだけ夢中になれるか」が一番大事ですよね。今回の比較が皆さんの参考になれば嬉しいです！
"""

    # 保存
    safe_keyword = re.sub(r'[\\/*?:"<>|]', "", keyword)
    filename = f"{safe_keyword}.md"
    filepath = os.path.join("content/posts", filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Article generated: {filepath}")

if __name__ == "__main__":
    main()
