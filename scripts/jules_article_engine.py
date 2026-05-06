import json
import os
import re
import random
from datetime import datetime
from read_raw import load_raw_data
from history_check import get_history
from internal_links import get_related_articles

def calculate_ivs(item):
    score = 3.8
    name = item.get('name', '')
    features = item.get('features', [])

    # Logic based on attributes
    if len(features) > 3: score += 0.5
    if any(k in name for k in ['知育', 'モンテッソーリ', 'STEM']): score += 0.4
    if item.get('price', 0) > 5000: score -= 0.2 # Pricey penalty

    return round(min(score, 5.0), 1)

def generate_pros_cons(item):
    features = item.get('features', [])
    pros = features[:2] if features else ["評価が高い", "定番商品"]
    cons = ["少し高価かも"] if item.get('price', 0) > 10000 else ["特になし"]
    return pros, cons

def main():
    raw_data = load_raw_data()
    if not raw_data:
        print("No raw data found")
        return

    amazon = raw_data.get("amazon", {})
    rakuten = raw_data.get("rakuten", {})
    keyword = amazon.get("keyword", "話題のアイテム")
    mode = amazon.get("mode", "daily_random")

    rakuten_items = rakuten.get("items", [])

    internal_links = get_related_articles(keyword)

    slug = "baby-toy"
    if "ラトル" in keyword: slug = "baby-rattle"
    elif "積み木" in keyword: slug = "building-blocks"

    article = {
        "slug": slug,
        "title": f"🧸 【徹底比較】{keyword}のおすすめランキング｜AIが選ぶ知育価値NO.1は？",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "mode": mode,
        "lead": f"育児に欠かせない「{keyword}」。どれを選べばいいか迷っていませんか？AI編集長Julesが、Amazon・楽天・Yahooのデータを解析し、本当に価値のある一品を厳選しました。",
        "products": [],
        "youtube_embeds": [],
        "internal_links": internal_links,
        "editorial_comment": f"{keyword}は、単なる遊び道具ではなく、親子の対話を深める魔法のツールです。長く愛せるものを選びたいですね。",
        "tags": [keyword, "知育玩具", "おすすめ"]
    }

    products = []
    for i, it in enumerate(amazon.get("items", [])):
        p, c = generate_pros_cons(it)
        # Match rakuten url (fallback to first available if mock, or empty)
        r_url = ""
        if i < len(rakuten_items):
            r_url = rakuten_items[i].get("url", "")

        products.append({
            "asin": it.get("asin"),
            "name": it.get("title"),
            "price": it.get("price"),
            "amazon_url": it.get("url"),
            "rakuten_url": r_url,
            "image": it.get("image"),
            "ivs_score": calculate_ivs(it),
            "pros": p,
            "cons": c,
            "features": it.get("features", [])
        })

    # Sort by IVS Score
    article["products"] = sorted(products, key=lambda x: x["ivs_score"], reverse=True)

    # YouTube (ID extraction)
    youtube = raw_data.get("youtube", {})
    for vid in youtube.get("items", [])[:2]:
        try:
            v_id = vid["url"].split("v=")[-1]
            article["youtube_embeds"].append(f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>')
        except: pass

    os.makedirs("data/articles", exist_ok=True)
    out_path = f"data/articles/{datetime.now().strftime('%Y-%m-%d')}-{article['slug']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=4)

    print(f"Evolved article JSON generated: {out_path}")

if __name__ == "__main__":
    main()
