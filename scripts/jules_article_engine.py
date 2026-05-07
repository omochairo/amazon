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
    tomy = raw_data.get("takaratomy", {})
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


    # Read signal if available
    signal_title = ""
    signal_type = "standard"
    try:
        with open("data/raw/top_signals.json", "r", encoding="utf-8") as f:
            sig = json.load(f)
            signal_title = sig.get("title", "")
            signal_type = sig.get("type", "standard")
    except: pass

    slug = "baby-toy"
    if "ラトル" in keyword: slug = "baby-rattle"
    elif "積み木" in keyword or "ブロック" in keyword: slug = "building-blocks"

    # Deep SEO Optimization Structure tailored by Signal
    if signal_type == "sudden_jump":
        title = f"【急上昇速報】昨日まで圏外だった「{signal_title[:15]}...」が突然売れ始めた理由は？"
        lead = f"楽天ランキングで異例の急上昇を記録した「{signal_title}」。なぜ今、爆発的に売れているのか？SNSの口コミや類似商品との比較から、その人気の秘密を徹底解剖します！"
    elif signal_type == "preorder":
        title = f"【予約完売注意】「{signal_title[:15]}...」の予約が開始！絶対に手に入れたい注目アイテムまとめ"
        lead = f"ファン待望の新作「{signal_title}」の予約がついに始まりました！発売直前にはプレミア化して手に入らなくなる可能性があるため、早めの確保がおすすめです。あわせてチェックしたい関連アイテムも厳選しました。"
    elif signal_type == "new_arrival":
        title = f"【初登場】市場が注目する最新おもちゃ「{signal_title[:15]}...」のポテンシャルとは？"
        lead = f"データ分析システムが市場に初登場したばかりの注目アイテム「{signal_title}」をキャッチしました！まだ誰も知らないこの最新アイテムの魅力と、ライバル商品とのスペック比較をお届けします。"
    else:
        title = f"【徹底比較】{keyword}のおすすめ人気ランキング厳選！失敗しない選び方"
        lead = f"育児に欠かせない「{keyword}」。種類が多すぎてどれを選べばいいか迷っていませんか？この記事では、Amazon・楽天・Yahoo!ショッピングから厳選した本当に価値のあるアイテムを徹底比較します。さらに、タカラトミーモール直送の最新情報も合わせてお届け！"

    article = {
        "slug": slug,
        "title": title,
        "meta_description": lead[:100] + "...",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "mode": mode,
        "lead": lead,
        "signal_type": signal_type if signal_type != "standard" else None,
        "signal_type_label": "急上昇" if signal_type == "sudden_jump" else "予約開始" if signal_type == "preorder" else "新着" if signal_type == "new_arrival" else "",
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

    # Takara Tomy Integration
    article["tomy_items"] = []
    for t in tomy.get("items", [])[:3]:
        article["tomy_items"].append({
            "title": t.get("title"),
            "url": t.get("url")
        })

    os.makedirs("data/articles", exist_ok=True)
    out_path = f"data/articles/{datetime.now().strftime('%Y-%m-%d')}-{article['slug']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=4)

    print(f"Evolved article JSON generated: {out_path}")

if __name__ == "__main__":
    main()
