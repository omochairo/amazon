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

def clean_title(text):
    """Removes marketing noise, brackets, and extra spaces from titles."""
    if not text: return ""

    # Pre-clean: Remove common dangling brackets often seen at start/end
    text = re.sub(r'^[】\]）\)]+', '', text)
    text = re.sub(r'[【\[（\(]+$', '', text)

    # Remove contents inside brackets 【】 [] （） () ＼／
    # Non-greedy match to handle multiple bracket sets correctly
    text = re.sub(r'[【\[（\(＼].*?[】\]）\)／]', ' ', text)

    # Remove any remaining standalone brackets that might be left from unbalanced strings
    text = re.sub(r'[【】\[\]（）\(\)＼／]', ' ', text)

    # Remove marketing keywords
    noise = [
        '送料無料', 'ポイント10倍', '楽天1位', '限定', 'クーポン', '正規品', '公式',
        '最新', '2024', '2025', '2026', '予約', 'おまけ付き', 'ラッピング無料',
        'あす楽', '即納'
    ]
    for n in noise:
        text = text.replace(n, ' ')

    # Strip symbols and replace with space
    text = re.sub(r'[!！?？@＠#＃$%&＊*+＋=＝_＿|｜\\＼/／:：;；"”''’`｀^＾~～]', ' ', text)

    # Cleanup whitespace
    text = " ".join(text.split())
    return text.strip()

def generate_slug(keyword):
    """Generates a URL-safe slug from a keyword."""
    # Convert to lowercase and replace spaces/special chars
    # We strictly use alphanumeric ASCII for broadest compatibility
    slug = keyword.lower()
    # Replace non-alphanumeric with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')

    # If slug is empty after filtering (e.g. only Japanese), use a unique fallback
    if not slug:
        import hashlib
        slug = hashlib.md5(keyword.encode()).hexdigest()[:8]

    # Prepend date
    date_str = datetime.now().strftime('%Y-%m-%d')
    return f"{date_str}-{slug}"

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

    clean_sig_title = clean_title(signal_title)
    # Deep SEO Optimization Structure tailored by Signal or Mode
    if signal_type == "sudden_jump":
        title = f"【急上昇速報】なぜ今「{clean_sig_title[:15]}」が売れている？人気の秘密を徹底解剖"
        lead = f"市場データが異常な売れ行きをキャッチ！現在、急激に注目を集めている「{clean_sig_title}」の魅力と、ライバル商品との比較結果をまとめました。"
    elif signal_type == "preorder":
        title = f"【予約開始】最新「{clean_sig_title[:15]}」を最安で手に入れる！スペック徹底比較レポート"
        lead = f"待望の新作「{clean_sig_title}」の予約がついに解禁。どこで買うのが一番お得か、主要3プラットフォームの価格と特典を調査しました。"
    elif signal_type == "new_arrival":
        title = f"【新着リサーチ】最新おもちゃ「{clean_sig_title[:15]}」の実力は？専門AIがスペック検証"
        lead = f"市場に登場したばかりの「{clean_sig_title}」。先行データから判明した知育効果と安全性を、既存の人気商品と比較してレポートします。"
    elif mode == "trend":
        title = f"【2026最新】{keyword}の徹底比較レポート｜今選ぶべきおすすめアイテム"
        lead = f"現在、SNSや市場で高く評価されている「{keyword}」をプロの視点で比較。失敗しないための選び方と、最も価値のある一品を特定します。"
    elif mode == "hidden_gem":
        title = f"【掘り出し物】コスパ最強の{keyword}はどれ？知る人ぞ知る名作をAIが特定"
        lead = f"有名ブランド以外にも優れた商品は存在します。価格以上の満足度（IVS）を叩き出した「{keyword}」の隠れた逸品をご紹介。"
    elif mode == "parenting":
        title = f"【実用性重視】パパ・ママが選ぶ「{keyword}」比較ガイド｜長く使える一品はこれ"
        lead = f"忙しい子育て世代のために、片付けやすさ、安全性、子供の食いつきを基準に「{keyword}」を徹底比較しました。"
    elif mode == "seasonal":
        title = f"【贈り物に】今プレゼントしたい{keyword}特集｜予算別・人気順比較"
        lead = f"大切な人へのギフトに最適な「{keyword}」。予算内で最高の結果を出せるアイテムを、市場価格データから厳選して提案します。"
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

    # Chunking logic: Split products into multiple smaller articles
    raw_items = amazon.get("items", [])
    chunk_size = 5
    item_chunks = [raw_items[i:i + chunk_size] for i in range(0, len(raw_items), chunk_size)]

    for idx, chunk in enumerate(item_chunks):
        current_products = []
        for it in chunk:
            p, c = generate_pros_cons(it)

            r_match = find_best_match(it.get("title", ""), rakuten_items)
            y_match = find_best_match(it.get("title", ""), yahoo_items)

            amazon_p = it.get("price") or 0
            rakuten_p = r_match.get("price") if r_match else 0
            yahoo_p = y_match.get("price") if y_match else 0

            prices = [p for p in [amazon_p, rakuten_p, yahoo_p] if p > 0]
            min_p = min(prices) if prices else 0
            best_platform = "Amazon"
            if min_p > 0:
                if min_p == rakuten_p: best_platform = "楽天"
                elif min_p == yahoo_p: best_platform = "Yahoo"

            ivs_base = calculate_ivs(it)
            current_products.append({
                "asin": it.get("asin"),
                "name": clean_title(it.get("title")),
                "price": amazon_p,
                "rakuten_price": rakuten_p,
                "yahoo_price": yahoo_p,
                "amazon_url": it.get("url"),
                "rakuten_url": r_match.get("url") if r_match else "",
                "yahoo_url": y_match.get("url") if y_match else "",
                "best_platform": best_platform,
                "price_diff_label": f"({best_platform}が最安)" if min_p < amazon_p else "(Amazonが最安)",
                "image": it.get("image") or (it.get("images")[0] if it.get("images") else ""),
                "ivs_score": ivs_base,
                "ivs_score_100": int(ivs_base * 20),
                "pros": p,
                "cons": c,
                "features": it.get("features", [])
            })

        # Create unique slug and title for each chunk
        chunk_slug = f"{slug}-part{idx+1}" if len(item_chunks) > 1 else slug
        chunk_title = f"{title} (Vol.{idx+1})" if len(item_chunks) > 1 else title

        current_article = {
            "slug": chunk_slug,
            "title": chunk_title,
            "meta_description": lead[:100] + "...",
            "date": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "mode": mode,
            "lead": lead,
            "signal_type": signal_type if signal_type != "standard" else None,
            "signal_type_label": "急上昇" if signal_type == "sudden_jump" else "予約開始" if signal_type == "preorder" else "新着" if signal_type == "new_arrival" else "",
            "products": sorted(current_products, key=lambda x: x["ivs_score"], reverse=True),
            "youtube_embeds": [],
            "books": [],
            "news": [],
            "internal_links": internal_links,
            "editorial_comment": f"{keyword}を選ぶ際のポイントは、子どもの月齢や興味に合っているかどうかです。長く愛せる一品を見つけて、親子の充実した時間を過ごしましょう。",
            "tags": [keyword, "知育玩具", "おすすめ", "徹底比較", "2026年最新"]
        }

        # Add editorial summary
        cheapest_count = sum(1 for p in current_products if "最安" in p.get("price_diff_label", ""))
        if cheapest_count > len(current_products) / 2:
            current_article["editorial_comment"] += f" 今回のラインナップではAmazonがお得な傾向にあります。"

        # Distribute YouTube/Books/News/Tomy across chunks
        for vid in youtube.get("items", [])[idx*2 : (idx+1)*2]:
            try:
                v_id = vid["url"].split("v=")[-1]
                current_article["youtube_embeds"].append({
                    "title": vid.get("title", "おすすめ動画"),
                    "embed_html": f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>'
                })
            except: pass

        for book in books_items[idx : idx+1]:
            current_article["books"].append({
                "title": book.get("title"),
                "url": book.get("url"),
                "image": book.get("image"),
                "description": book.get("description", "")[:100] + "..."
            })

        for n in news.get("items", [])[idx : idx+1]:
            current_article["news"].append({
                "title": n.get("title"),
                "url": n.get("url")
            })

        current_article["tomy_items"] = []

        os.makedirs("data/articles", exist_ok=True)
        out_path = f"data/articles/{current_article['slug']}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(current_article, f, ensure_ascii=False, indent=4)

        print(f"Generated: {out_path}")

if __name__ == "__main__":
    main()
