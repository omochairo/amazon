import json
import os
import re
import pathlib
from datetime import datetime
from read_raw import load_raw_data
from history_check import get_history
from internal_links import get_related_articles


# --- ノイズワード（マッチングから除外する汎用語句）---
NOISE_WORDS = frozenset([
    '知育玩具', 'おもちゃ', 'プレゼント', '誕生日', 'ギフト', '男の子', '女の子',
    '子供', '子ども', 'こども', '幼児', '赤ちゃん', 'ベビー', 'キッズ',
    '送料無料', '対象年齢', '以上', '歳', '玩具', '遊び', '知育',
    'おすすめ', '人気', '出産祝い', 'クリスマス', '入園', '入学',
    '男', '女', 'お祝い', '新品', '正規品', '日本', '木製', '教育',
    'セット', 'おもしろ', '楽しい', '安全', '安心', 'ブロック',
    'toy', 'kids', 'baby', 'gift', 'present', 'boy', 'girl',
])


def extract_product_tokens(title):
    """商品名から固有性の高いトークンを抽出する。"""
    if not title:
        return set()
    # Unicode文字/英数字トークンを抽出
    tokens = set(re.findall(r'[一-龥ぁ-んァ-ヴー]{2,}|[a-zA-Z0-9]{2,}', title))
    # ノイズワード除去
    return tokens - NOISE_WORDS


def calculate_ivs(item):
    """IVS (知育価値スコア) を算出。"""
    name = item.get('name', item.get('title', ''))
    features = " ".join(item.get('features', []))
    price = item.get('price', 0) or 0

    edu = 3.0
    edu_kw = ['知育', 'モンテッソーリ', 'STEM', 'プログラミング', 'ブロック',
              '思考力', '想像力', '空間把握', '学習', '教育']
    edu += sum(0.3 for k in edu_kw if k in name or k in features)
    edu = min(edu, 5.0)

    lon = 3.0
    lon_kw = ['長く遊べる', '成長に合わせて', '歳以上', '全年齢', '丈夫', '耐久']
    lon += sum(0.4 for k in lon_kw if k in features)
    lon = min(lon, 5.0)

    safe = 3.0
    safe_kw = ['安全', 'STマーク', '自然由来', '食品衛生法', '角を丸く',
               'なめても安心', '無毒', '食品衛生']
    safe += sum(0.5 for k in safe_kw if k in features)
    safe = min(safe, 5.0)

    cp = 3.0
    if price > 0:
        if price < 2000: cp = 4.5
        elif price < 4000: cp = 4.0
        elif price < 7000: cp = 3.0
        elif price < 12000: cp = 2.0
        else: cp = 1.5

    correction = 1.0
    rc = item.get('reviewCount', 0)
    if rc > 500: correction = 1.1
    elif rc > 100: correction = 1.05
    elif rc < 10: correction = 0.9

    raw = ((edu * lon) + safe) / (6 - cp) * correction
    score5 = round(min(max((raw / 10) * 2.5 + 2.5, 1.0), 5.0), 1)

    detail = {
        "education": round(edu, 1),
        "longevity": round(lon, 1),
        "safety": round(safe, 1),
        "cost_performance": round(cp, 1),
        "total": score5,
        "total_100": int(score5 * 20),
    }
    return score5, detail


def generate_pros_cons(item):
    features = item.get('features', [])
    full = " ".join(features) + item.get('title', '')

    pros = []
    if any(k in full for k in ['安全', 'STマーク', 'なめても安心', '自然由来', '食品衛生']):
        pros.append("高い安全性")
    if any(k in full for k in ['長く遊べる', '成長に合わせて', '幅広い年齢']):
        pros.append("長く愛用できる")
    if any(k in full for k in ['簡単', 'すぐ遊べる', 'シンプル', 'タッチ']):
        pros.append("直感的に遊べる")
    if any(k in full for k in ['知育', '学習', '思考力', '創造力', '教育']):
        pros.append("知育効果が高い")
    if any(k in full for k in ['グッドトイ', '受賞', '大賞']):
        pros.append("受賞歴あり")
    if not pros:
        pros = ["評価が高い", "定番商品"]

    cons = []
    if item.get('price', 0) > 10000:
        cons.append("やや高価")
    if any(k in full for k in ['難しい', '大人と一緒', '複雑']):
        cons.append("低年齢児には少し難しい")
    if any(k in full for k in ['電池', '別売り']):
        cons.append("電池が別途必要")
    if not cons:
        cons = ["特になし"]
    return pros[:3], cons[:2]


def find_best_match(target_title, pool_items, min_overlap=3):
    """
    商品タイトルの固有トークンマッチング。
    ノイズワードを除去した上でブランド名・商品固有名で一致を判定。
    min_overlap=3 により、少なくとも3つの固有トークンが一致する必要がある。
    """
    if not pool_items:
        return None
    t_tokens = extract_product_tokens(target_title)
    if len(t_tokens) < 2:
        return None

    best, best_score, best_ratio = None, 0, 0.0
    for item in pool_items:
        p_tokens = extract_product_tokens(item.get("title", ""))
        overlap = t_tokens & p_tokens
        score = len(overlap)
        # 重複トークンの割合も考慮（ターゲット側の何%がマッチしたか）
        ratio = score / len(t_tokens) if t_tokens else 0

        if score >= min_overlap and (score > best_score or
                                     (score == best_score and ratio > best_ratio)):
            best_score = score
            best_ratio = ratio
            best = item
    return best


def find_related_videos(product_title, videos, max_results=2):
    """商品タイトルの固有キーワードで関連動画をマッチング。"""
    if not videos:
        return []
    t_tokens = extract_product_tokens(product_title)
    scored = []
    for vid in videos:
        v_tokens = extract_product_tokens(vid.get("title", ""))
        score = len(t_tokens & v_tokens)
        if score >= 2:
            scored.append((score, vid))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored[:max_results]]


def make_search_url(platform, keyword):
    """マッチしなかった場合の検索リンクを生成。"""
    import urllib.parse
    q = urllib.parse.quote(keyword[:30])
    if platform == "rakuten":
        return f"https://search.rakuten.co.jp/search/mall/{q}/"
    elif platform == "yahoo":
        return f"https://shopping.yahoo.co.jp/search?p={q}"
    return ""


def clean_title(text):
    if not text:
        return ""
    text = re.sub(r'[【\[（\(＼].*?[】\]）\)／]', ' ', text)
    text = re.sub(r'[【】\[\]（）\(\)＼／]', ' ', text)
    noise = ['送料無料', 'ポイント10倍', '楽天1位', '限定', 'クーポン',
             '正規品', '公式', '最新', '予約', 'おまけ付き', 'ラッピング無料',
             'あす楽', '即納']
    for n in noise:
        text = text.replace(n, ' ')
    return " ".join(text.split()).strip()


def main():
    raw = load_raw_data()
    if not raw:
        print("No raw data found")
        return

    amazon_items = raw.get("amazon", {}).get("items", [])
    rakuten_items = raw.get("rakuten", {}).get("items", [])
    yahoo_items = raw.get("yahoo_result", raw.get("yahoo", {})).get("items", [])
    youtube_items = raw.get("youtube", {}).get("items", [])
    news_items = raw.get("news", {}).get("items", [])
    books_items = raw.get("books_result", raw.get("books", {})).get("items", [])

    if not amazon_items:
        print("No Amazon items to process")
        return

    # 既存記事をチェック（重複防止）
    articles_dir = pathlib.Path("data/articles")
    existing_slugs = set()
    if articles_dir.exists():
        for f in articles_dir.glob("*.json"):
            existing_slugs.add(f.stem)

    today = datetime.now().strftime('%Y-%m-%d')
    generated = 0

    for item in amazon_items:
        asin = item.get("asin", "")
        if not asin:
            continue

        slug = f"{today}-{asin}"
        if slug in existing_slugs:
            print(f"Skip (already exists): {slug}")
            continue

        title_raw = item.get("title", "")
        title_clean = clean_title(title_raw)
        price_amazon = item.get("price", 0) or 0

        # --- 楽天・Yahoo マッチング（固有トークンベース）---
        r_match = find_best_match(title_raw, rakuten_items)
        y_match = find_best_match(title_raw, yahoo_items)

        # マッチ結果の信頼度ログ
        if r_match:
            r_tokens = extract_product_tokens(title_raw) & extract_product_tokens(r_match.get("title", ""))
            print(f"  Rakuten match: {len(r_tokens)} tokens ({', '.join(list(r_tokens)[:5])})")
        if y_match:
            y_tokens = extract_product_tokens(title_raw) & extract_product_tokens(y_match.get("title", ""))
            print(f"  Yahoo match: {len(y_tokens)} tokens ({', '.join(list(y_tokens)[:5])})")

        price_rakuten = r_match.get("price", 0) if r_match else 0
        price_yahoo = y_match.get("price", 0) if y_match else 0

        # マッチしなかった場合は検索リンクを使用
        rakuten_url = r_match.get("url", "") if r_match else make_search_url("rakuten", title_clean)
        yahoo_url = y_match.get("url", "") if y_match else make_search_url("yahoo", title_clean)
        rakuten_label = r_match.get("title", "") if r_match else "楽天で検索"
        yahoo_label = y_match.get("title", "") if y_match else "Yahoo!で検索"

        # 最安プラットフォーム判定
        prices = {}
        if price_amazon > 0:
            prices["Amazon"] = price_amazon
        if price_rakuten > 0:
            prices["楽天"] = price_rakuten
        if price_yahoo > 0:
            prices["Yahoo"] = price_yahoo

        if prices:
            best_platform = min(prices, key=prices.get)
            best_price = prices[best_platform]
        else:
            best_platform = "Amazon"
            best_price = price_amazon

        # IVSスコア
        ivs_score, ivs_detail = calculate_ivs(item)

        # メリット・デメリット
        pros, cons = generate_pros_cons(item)

        # 関連動画
        related_vids = find_related_videos(title_raw, youtube_items)
        youtube_embeds = []
        for vid in related_vids:
            try:
                v_id = vid["url"].split("v=")[-1]
                youtube_embeds.append({
                    "title": vid.get("title", ""),
                    "thumbnail": vid.get("thumbnail", ""),
                    "embed_html": f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>'
                })
            except Exception:
                pass

        # 関連ニュース
        related_news = []
        for n in news_items[:2]:
            related_news.append({
                "title": n.get("title", ""),
                "url": n.get("url", ""),
            })

        # 関連書籍
        related_books = []
        b_match = find_best_match(title_raw, books_items, min_overlap=2)
        if b_match:
            related_books.append({
                "title": b_match.get("title", ""),
                "url": b_match.get("url", ""),
                "image": b_match.get("image", ""),
            })

        # 内部リンク
        internal_links = get_related_articles(title_clean)

        # 記事タイトル生成（商品名を先頭に）
        short_name = title_clean[:25]
        article_title = f"【価格比較】{short_name}｜Amazon・楽天・Yahoo最安値ガイド"

        article = {
            "slug": slug,
            "title": article_title,
            "meta_description": f"{title_clean[:40]}をAmazon・楽天・Yahooで徹底比較。最安値・IVSスコア・関連動画つき購入ガイド。",
            "date": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "tags": [title_clean[:10], "価格比較", "購入ガイド", "知育玩具"],
            "product": {
                "asin": asin,
                "name": title_clean,
                "name_full": title_raw,
                "image": item.get("image", ""),
                "features": item.get("features", []),
                "pros": pros,
                "cons": cons,
                "ivs_score": ivs_score,
                "ivs_detail": ivs_detail,
                "prices": {
                    "amazon": {
                        "price": price_amazon,
                        "url": item.get("url", ""),
                    },
                    "rakuten": {
                        "price": price_rakuten,
                        "url": rakuten_url,
                        "matched_title": rakuten_label,
                        "is_search": r_match is None,
                    },
                    "yahoo": {
                        "price": price_yahoo,
                        "url": yahoo_url,
                        "matched_title": yahoo_label,
                        "is_search": y_match is None,
                    },
                },
                "best_platform": best_platform,
                "best_price": best_price,
            },
            "youtube_embeds": youtube_embeds,
            "news": related_news,
            "books": related_books,
            "internal_links": internal_links,
            "editorial_comment": f"「{title_clean[:15]}」は{best_platform}で購入するのが現時点で最もお得です。" if best_price > 0 else f"「{title_clean[:15]}」の詳細な価格比較をお届けしました。",
        }

        # 保存
        articles_dir.mkdir(parents=True, exist_ok=True)
        out_path = articles_dir / f"{slug}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=4)
        print(f"Generated: {out_path}")
        generated += 1

    print(f"\nTotal: {generated} articles generated")


if __name__ == "__main__":
    main()
