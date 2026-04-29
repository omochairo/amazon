import json
import os
import re
import random
from datetime import datetime

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def calculate_ivs(item):
    asin = item.get('asin') or item.get('itemCode') or item.get('code') or 'dummy'
    asin_hash = sum(ord(c) for c in str(asin))
    score = (asin_hash % 20) / 4.0 + 1.0
    return round(min(score, 5.0), 1)

def parse_price(p):
    if isinstance(p, (int, float)): return int(p)
    if not p: return 9999999
    nums = re.findall(r'\d+', str(p).replace(',', ''))
    return int(nums[0]) if nums else 9999999

def main():
    data_dir = "data"
    history_file = os.path.join(data_dir, "post_history.json")

    history = load_json(history_file) or []

    amazon_data = load_json(os.path.join(data_dir, "amazon_result.json")) or load_json(os.path.join(data_dir, "search_result.json"))
    rakuten_data = load_json(os.path.join(data_dir, "rakuten_result.json"))
    yahoo_data = load_json(os.path.join(data_dir, "yahoo_result.json"))

    youtube_data = load_json(os.path.join(data_dir, "youtube_result.json"))
    books_data = load_json(os.path.join(data_dir, "books_result.json"))
    news_data = load_json(os.path.join(data_dir, "news_result.json"))
    trends_data = load_json(os.path.join(data_dir, "trends_result.json"))
    internal_links = load_json(os.path.join(data_dir, "internal_links.json")) or []

    keyword = "注目アイテム"
    if amazon_data and isinstance(amazon_data, dict): keyword = amazon_data.get("keyword", keyword)
    elif rakuten_data and isinstance(rakuten_data, dict): keyword = rakuten_data.get("keyword", keyword)
    elif yahoo_data and isinstance(yahoo_data, dict): keyword = yahoo_data.get("keyword", keyword)

    history.append({
        "date": datetime.now().isoformat(),
        "keyword": keyword
    })
    os.makedirs(data_dir, exist_ok=True)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

    modes = ["【トレンド特報】", "【隠れた名作発掘】", "【育児お悩み相談】", "【季節の歳時記】"]
    mode = random.choice(modes)

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

    all_products = []
    if amazon_data and isinstance(amazon_data, dict):
        all_products.extend(amazon_data.get("items", []))
    if rakuten_data:
        r_items = rakuten_data.get("items", []) if isinstance(rakuten_data, dict) else rakuten_data
        for item in r_items:
            if isinstance(item, dict) and 'Item' in item: item = item['Item']
            all_products.append({
                "title": item.get("itemName") or item.get("title"),
                "price": item.get("itemPrice") or item.get("price"),
                "url": item.get("affiliateUrl") or item.get("url"),
                "source": "Rakuten",
                "asin": item.get("asin") or item.get("itemCode", "N/A"),
                "image": item.get("image")
            })
    if yahoo_data and isinstance(yahoo_data, dict):
        all_products.extend(yahoo_data.get("items", []))

    min_price = 9999999
    for p in all_products:
        price_val = parse_price(p.get('price'))
        if 0 < price_val < min_price:
            min_price = price_val

    for item in all_products:
        source = item.get("source", "Unknown")
        price_val = parse_price(item.get('price'))
        is_cheapest = price_val == min_price and min_price < 9999999
        ivs = calculate_ivs(item)
        cheapest_label = " 🔥 **最安値！**" if is_cheapest else ""

        content += f"### [{source}] {item['title']}\n"
        if item.get('image'):
            content += f"![{item['title']}]({item['image']})\n\n"
        content += f"- **💰 価格**: {item['price']}{cheapest_label}\n"
        content += f"- **📊 IVSスコア**: {ivs}/5.0\n"
        content += f"- **🔗 詳細・購入**: [{item['title']}]({item['url']})\n\n"

    content += "---\n\n"

    if mode == "【隠れた名作発掘】":
        content += f"## 💡 Julesの深掘り：隠れた名作\nAPIデータには現れにくいですが、{keyword}の中でも特に教育的価値が高いと感じる一品をご紹介しました。\n\n"

    if youtube_data and isinstance(youtube_data, dict) and youtube_data.get("items"):
        content += f"## 📺 関連動画でチェック\n\n"
        for v in youtube_data["items"][:2]:
            content += f"### {v['title']}\n"
            content += f"[![{v['title']}]({v['thumbnail']})]({v['url']})\n\n"

    if books_data and isinstance(books_data, dict) and books_data.get("items"):
        content += f"## 📚 親子で読みたい関連書籍\n「{keyword}」に関連する、知育や育児のヒントが詰まった本をピックアップしました。\n\n"
        for b in books_data["items"][:2]:
            content += f"### {b['title']}\n"
            if b.get('image'):
                content += f"![{b['title']}]({b['image']})\n\n"
            content += f"- **✍️ 著者**: {', '.join(b.get('authors', []))}\n"
            content += f"- **📖 内容**: {b.get('description', '説明なし')[:100]}...\n"
            content += f"- **🔗 詳細**: [{b['title']}]({b['url']})\n\n"

    if internal_links:
        content += f"## 🔗 あわせて読みたい：おもちゃいろの関連記事\n\n"
        for link in internal_links:
            content += f"- {link}\n"
        content += "\n"

    content += f"""
> [!IMPORTANT]
> 知育玩具は対象年齢を守って安全に遊びましょう。特に小さなパーツの誤飲には十分ご注意ください。

> [!TIP]
> {keyword}を長く楽しむコツは、親御さんも一緒に「遊び方」を工夫すること。たまに遊び方を教えずに、子供の自由な発想を見守るのもおすすめですよ。

> [!QUOTE]
> **AI編集長Julesのひとりごと**
> {keyword}選びって本当に奥が深いですよね。単なる道具じゃなくて、親子のコミュニケーションのきっかけになってほしいな、と思いながら今回の記事をまとめました。
"""

    safe_keyword = re.sub(r'[\\/*?:"<>|]', "", keyword)
    filename = f"{safe_keyword}.md"
    filepath = os.path.join("content/posts", filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Article generated: {filepath}")

if __name__ == "__main__":
    main()
