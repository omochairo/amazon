import json
import os
import re
import random
from datetime import datetime
from read_raw import load_raw_data
from history_check import get_history
from internal_links import get_related_articles

def calculate_ivs(item):
    """
    Calculates the Intelligent Value Score (IVS) based on the formula:
    IVS = ((Educational Effect * Longevity) + Safety Score) / Cost Performance * Correction Factor
    """
    name = item.get('name', item.get('title', ''))
    features = " ".join(item.get('features', []))
    price = item.get('price', 0) or 0

    # 1. Educational Effect (1.0 - 5.0)
    edu_effect = 3.0
    edu_keywords = ['知育', 'モンテッソーリ', 'STEM', 'プログラミング', 'ブロック', '思考力', '想像力', '空間把握']
    edu_effect += sum(0.3 for k in edu_keywords if k in name or k in features)
    edu_effect = min(edu_effect, 5.0)

    # 2. Longevity (1.0 - 5.0)
    longevity = 3.0
    long_keywords = ['長く遊べる', '成長に合わせて', '～', '歳以上', '全年齢', '丈夫']
    longevity += sum(0.4 for k in long_keywords if k in features)
    longevity = min(longevity, 5.0)

    # 3. Safety Score (1.0 - 5.0)
    safety = 3.0
    safe_keywords = ['安全', 'STマーク', '自然由来', '食品衛生法', '角を丸く', 'なめても安心', '無毒']
    safety += sum(0.5 for k in safe_keywords if k in features)
    safety = min(safety, 5.0)

    # 4. Cost Performance (1.0 - 5.0, where lower price is better CP)
    # Assume 3000-5000 yen is 'average' (3.0)
    cp = 3.0
    if price > 0:
        if price < 2000: cp = 4.5
        elif price < 4000: cp = 4.0
        elif price < 7000: cp = 3.0
        elif price < 12000: cp = 2.0
        else: cp = 1.5

    # 5. Correction Factor (Review count / popularity)
    correction = 1.0
    review_count = item.get('reviewCount', 0)
    if review_count > 500: correction = 1.1
    elif review_count > 100: correction = 1.05
    elif review_count < 10: correction = 0.9

    # Final Calculation
    # IVS = ((edu * long) + safety) / (6 - cp) * correction
    # (6-cp) makes it so higher CP (lower price) results in higher score
    raw_score = ((edu_effect * longevity) + safety) / (6 - cp) * correction

    # Normalize to 0-5 range (roughly)
    final_score = (raw_score / 10) * 2.5 + 2.5

    return round(min(max(final_score, 1.0), 5.0), 1)

def generate_pros_cons(item):
    features = item.get('features', [])
    full_text = " ".join(features) + item.get('title', '')

    pros = []
    if any(k in full_text for k in ['安全', 'STマーク', 'なめても安心', '自然由来']):
        pros.append("高い安全性")
    if any(k in full_text for k in ['長く遊べる', '成長に合わせて', '幅広い年齢']):
        pros.append("長く愛用できる")
    if any(k in full_text for k in ['簡単', 'すぐ遊べる', 'シンプル']):
        pros.append("直感的に遊べる")
    if any(k in full_text for k in ['知育', '学習', '思考力', '創造力']):
        pros.append("知育効果が高い")

    if not pros:
        pros = ["評価が高い", "定番商品"]

    cons = []
    if item.get('price', 0) > 10000:
        cons.append("やや高価")
    if any(k in full_text for k in ['難しい', '大人と一緒', '複雑']):
        cons.append("低年齢児には少し難しい")
    if any(k in full_text for k in ['電池', '別売り']):
        cons.append("電池が別途必要")

    if not cons:
        cons = ["特になし"]

    return pros[:3], cons[:2]

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

def generate_slug(keyword):
    """Generates a URL-safe slug from a keyword."""
    # Convert to lowercase and replace spaces/special chars
    # We strictly use alphanumeric ASCII for broadest compatibility
    slug = keyword.lower()
    # Replace non-alphanumeric with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')

    # If slug is empty after filtering (e.g. only Japanese), use a fallback
    if not slug:
        slug = "toy-review"

    # Prepend date
    date_str = datetime.now().strftime('%Y-%m-%d')
    return f"{date_str}-{slug}"

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

    # Mode selection: prioritizes explicit mode, otherwise picks one
    modes = ['trend', 'hidden_gem', 'parenting', 'seasonal']
    mode = amazon.get("mode")
    if mode not in modes:
        mode = random.choice(modes)

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

    slug = generate_slug(keyword)

    # Deep SEO Optimization Structure tailored by Signal or Mode
    if signal_type == "sudden_jump":
        title = f"【急上昇速報】昨日まで圏外だった「{signal_title[:15]}...」が突然売れ始めた理由は？"
        lead = f"楽天ランキングで異例の急上昇を記録した「{signal_title}」。なぜ今、爆発的に売れているのか？SNSの口コミや類似商品との比較から、その人気の秘密を徹底解剖します！"
    elif signal_type == "preorder":
        title = f"【予約完売注意】「{signal_title[:15]}...」の予約が開始！絶対に手に入れたい注目アイテムまとめ"
        lead = f"ファン待望の新作「{signal_title}」の予約がついに始まりました！発売直前にはプレミア化して手に入らなくなる可能性があるため、早めの確保がおすすめです。あわせてチェックしたい関連アイテムも厳選しました。"
    elif signal_type == "new_arrival":
        title = f"【初登場】市場が注目する最新おもちゃ「{signal_title[:15]}...」のポテンシャルとは？"
        lead = f"データ分析システムが市場に初登場したばかりの注目アイテム「{signal_title}」をキャッチしました！まだ誰も知らないこの最新アイテムの魅力と、ライバル商品とのスペック比較をお届けします。"
    elif mode == "trend":
        title = f"【2026年最新トレンド】今売れている{keyword}ランキング！人気の理由をプロが解説"
        lead = f"今、SNSや育児コミュニティで話題沸騰中の「{keyword}」。なぜこれほどまでに注目されているのか、最新の販売データからその魅力を紐解きます。"
    elif mode == "hidden_gem":
        title = f"【知る人ぞ知る名作】{keyword}の隠れた逸品を見つけました。コスパ最強の選択肢とは？"
        lead = f"有名ブランドではないけれど、実は高い知育効果と安全性を兼ね備えた「{keyword}」。そんな掘り出し物アイテムを厳選してご紹介します。"
    elif mode == "parenting":
        title = f"【現役パパママが選ぶ】{keyword}選びで後悔しないためのポイントとおすすめ10選"
        lead = f"毎日忙しいパパ・ママに贈る、実生活で本当に役立つ「{keyword}」ガイド。長く使えて、子どもが夢中になるアイテムだけをピックアップしました。"
    elif mode == "seasonal":
        title = f"【季節のおすすめ】今この時期に贈りたい、特別な{keyword}特集"
        lead = f"季節の行事やプレゼントにぴったりの「{keyword}」。今しか買えない注目アイテムや、ギフトに最適なセットをまとめました。"
    else:
        title = f"【徹底比較】{keyword}のおすすめ人気ランキング厳選！失敗しない選び方"
        lead = f"育児に欠かせない「{keyword}」。種類が多すぎてどれを選べばいいか迷っていませんか？この記事では、Amazon・楽天・Yahoo!ショッピングから厳選した本当に価値のあるアイテムを徹底比較します。"

    # Deep SEO Optimization Structure
    article = {
        "slug": slug,
        "title": title,
        "meta_description": lead[:100] + "...",
        "date": datetime.now().astimezone().replace(microsecond=0).isoformat(),
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

        amazon_p = it.get("price") or 0
        rakuten_p = r_match.get("price") if r_match else 0
        yahoo_p = y_match.get("price") if y_match else 0

        # Cross-reference analysis
        prices = [p for p in [amazon_p, rakuten_p, yahoo_p] if p > 0]
        min_p = min(prices) if prices else 0
        best_platform = "Amazon"
        if min_p > 0:
            if min_p == rakuten_p: best_platform = "楽天"
            elif min_p == yahoo_p: best_platform = "Yahoo"

        products.append({
            "asin": it.get("asin"),
            "name": it.get("title"),
            "price": amazon_p,
            "rakuten_price": rakuten_p,
            "yahoo_price": yahoo_p,
            "amazon_url": it.get("url"),
            "rakuten_url": r_match.get("url") if r_match else "",
            "yahoo_url": y_match.get("url") if y_match else "",
            "best_platform": best_platform,
            "price_diff_label": f"({best_platform}が最安)" if min_p < amazon_p else "(Amazonが最安)",
            "image": it.get("image") or (it.get("images")[0] if it.get("images") else ""),
            "ivs_score": calculate_ivs(it),
            "pros": p,
            "cons": c,
            "features": it.get("features", [])
        })

    # Sort by IVS Score
    article["products"] = sorted(products, key=lambda x: x["ivs_score"], reverse=True)

    # Cross-reference analysis summary (Layer 2 Editorial logic)
    cheapest_count = sum(1 for p in products if "最安" in p.get("price_diff_label", ""))
    if cheapest_count > len(products) / 2:
        article["editorial_comment"] += f" 今回調査した中ではAmazonが全体的に低価格な傾向にありました。"
    elif cheapest_count < len(products) / 3:
        article["editorial_comment"] += f" 楽天やYahooショッピングの方がお得なケースが多いようです。ポイント還元も含めて検討しましょう。"

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
    out_path = f"data/articles/{article['slug']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=4)

    print(f"Evolved article JSON generated: {out_path}")

if __name__ == "__main__":
    main()
