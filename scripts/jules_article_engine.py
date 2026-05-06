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
    name = item.get('name', item.get('title', ''))
    features = item.get('features', [])

    # Logic based on attributes
    if len(features) > 3: score += 0.5
    if any(k in name for k in ['知育', 'モンテッソーリ', 'STEM', 'プログラミング', 'ブロック']): score += 0.4
    if item.get('price', 0) > 5000: score -= 0.2 # Pricey penalty
    if item.get('reviewCount', 0) > 100: score += 0.3

    return round(min(score, 5.0), 1)

def generate_pros_cons(item):
    features = item.get('features', [])
    pros = features[:2] if features else ["評価が高い", "定番商品"]
    cons = ["少し高価かも"] if item.get('price', 0) > 10000 else ["特になし"]
    return pros, cons

def find_best_match(target_title, pool_items):
    """Finds the best matching product from another API pool based on simple token overlap."""
    if not pool_items: return None
    best_item = None
    best_score = 0
    target_tokens = set(re.findall(r'[一-龥ぁ-んァ-ンa-zA-Z0-9]+', target_title))

    for item in pool_items:
        pool_title = item.get("title", "")
        pool_tokens = set(re.findall(r'[一-龥ぁ-んァ-ンa-zA-Z0-9]+', pool_title))
        score = len(target_tokens.intersection(pool_tokens))
        if score > best_score and score >= 2: # At least 2 words match
            best_score = score
            best_item = item

    return best_item

def main():
    raw_data = load_raw_data()
    if not raw_data:
        print("No raw data found")
        return

    amazon = raw_data.get("amazon", {})
    rakuten = raw_data.get("rakuten", {})
    yahoo = raw_data.get("yahoo", {})
    youtube = raw_data.get("youtube", {})
    news = raw_data.get("news", {})
    books = raw_data.get("books", {})

    keyword = amazon.get("keyword", "話題のアイテム")
    mode = amazon.get("mode", "daily_random")

    rakuten_items = rakuten.get("items", [])
    yahoo_items = yahoo.get("items", [])
    books_items = books.get("items", [])

    internal_links = get_related_articles(keyword)

    slug = "baby-toy"
    if "ラトル" in keyword: slug = "baby-rattle"
    elif "積み木" in keyword or "ブロック" in keyword: slug = "building-blocks"

    # Deep SEO Optimization Structure
    article = {
        "slug": slug,
        "title": f"【徹底比較】{keyword}のおすすめ人気ランキング厳選！失敗しない選び方",
        "meta_description": f"AIが厳選した{keyword}のおすすめ人気ランキングを大公開！Amazon・楽天・Yahooの口コミや最安値情報から、本当に選ぶべき一品を分かりやすく比較解説します。",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "mode": mode,
        "lead": f"育児に欠かせない「{keyword}」。種類が多すぎてどれを選べばいいか迷っていませんか？この記事では、Amazon・楽天・Yahoo!ショッピングから厳選した本当に価値のあるアイテムを徹底比較します。",
        "products": [],
        "youtube_embeds": [],
        "books": [],
        "news": [],
        "internal_links": internal_links,
        "editorial_comment": f"{keyword}を選ぶ際のポイントは、子どもの月齢や興味に合っているかどうかです。長く愛せる一品を見つけて、親子の充実した時間を過ごしましょう。",
        "tags": [keyword, "知育玩具", "おすすめ", "徹底比較", "2026年最新"]
    }

    # Process Products (Amazon is base)
    products = []
    for it in amazon.get("items", []):
        p, c = generate_pros_cons(it)

        # Cross-match with Rakuten and Yahoo for unified affiliate links
        r_match = find_best_match(it.get("title", ""), rakuten_items)
        y_match = find_best_match(it.get("title", ""), yahoo_items)

        products.append({
            "asin": it.get("asin"),
            "name": it.get("title"),
            "price": it.get("price"),
            "amazon_url": it.get("url"),
            "rakuten_url": r_match.get("url") if r_match else "",
            "yahoo_url": y_match.get("url") if y_match else "",
            "image": it.get("image"),
            "ivs_score": calculate_ivs(it),
            "pros": p,
            "cons": c,
            "features": it.get("features", [])
        })

    # Sort by IVS Score
    article["products"] = sorted(products, key=lambda x: x["ivs_score"], reverse=True)

    # YouTube (ID extraction)
    for vid in youtube.get("items", [])[:3]: # Up to 3 videos
        try:
            v_id = vid["url"].split("v=")[-1]
            article["youtube_embeds"].append({
                "title": vid.get("title", "おすすめ動画"),
                "embed_html": f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>'
            })
        except: pass

    # Books Integration
    for book in books_items[:3]:
        article["books"].append({
            "title": book.get("title"),
            "url": book.get("url"),
            "image": book.get("image"),
            "description": book.get("description", "")[:100] + "..."
        })

    # News Integration
    for n in news.get("items", [])[:3]:
        article["news"].append({
            "title": n.get("title"),
            "url": n.get("url")
        })

    os.makedirs("data/articles", exist_ok=True)
    out_path = f"data/articles/{datetime.now().strftime('%Y-%m-%d')}-{article['slug']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=4)

    print(f"Evolved article JSON generated: {out_path}")

if __name__ == "__main__":
    main()
