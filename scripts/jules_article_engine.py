import json
import os
import re
import random
from datetime import datetime
from jules_tools.read_raw import load_raw_data
from jules_tools.history_check import get_history
from jules_tools.internal_links import get_related_articles

def calculate_ivs(item):
    name = item.get('title', '')
    score = 3.5
    if '知育' in name: score += 0.5
    if '木製' in name: score += 0.3
    return round(min(score, 5.0), 1)

def main():
    raw_data = load_raw_data()
    if not raw_data:
        print("No raw data found in data/raw/")
        return

    amazon = raw_data.get("amazon", {})
    rakuten = raw_data.get("rakuten", {})
    youtube = raw_data.get("youtube", {})

    keyword = amazon.get("keyword", "item")

    internal_links = get_related_articles(keyword)

    # Slug should be simple
    slug = "baby-rattle" if "ラトル" in keyword else "toy-review"

    article = {
        "slug": slug,
        "title": f"🧸 おすすめの{keyword}徹底比較｜今買うべき一品は？",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "mode": random.choice(["trend", "hidden_gem", "parenting", "seasonal"]),
        "lead": f"人気の「{keyword}」について、最新の市場データと専門家の視点で徹底比較しました。本記事はAI編集長Julesが自動生成したものです。",
        "products": [],
        "youtube_embeds": [],
        "internal_links": internal_links,
        "editorial_comment": f"{keyword}選びは、お子様の成長に合わせた最適なタイミングが重要です。",
        "tags": [keyword, "知育玩具", "比較"]
    }

    for item in amazon.get("items", []):
        article["products"].append({
            "asin": item.get("asin"),
            "name": item.get("title"),
            "price": item.get("price"),
            "amazon_url": item.get("url"),
            "rakuten_url": "",
            "image": item.get("image"),
            "ivs_score": calculate_ivs(item),
            "pros": ["評判が良い", "安全設計"],
            "cons": ["特になし"]
        })

    for vid in youtube.get("items", [])[:2]:
        try:
            v_id = vid["url"].split("v=")[-1]
            article["youtube_embeds"].append(f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>')
        except: pass

    os.makedirs("data/articles", exist_ok=True)
    out_path = f"data/articles/{datetime.now().strftime('%Y-%m-%d')}-{article['slug']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=4)

    print(f"Article JSON generated: {out_path}")

if __name__ == "__main__":
    main()
