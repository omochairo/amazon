import json
import os
import re
import pathlib
import urllib.parse
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
    'セット', 'おもしろ', '楽しい', '安全', '安心',
    'toy', 'kids', 'baby', 'gift', 'present', 'boy', 'girl',
])


def extract_product_tokens(title):
    """商品名から固有性の高いトークンを抽出する。"""
    if not title:
        return set()
    tokens = set(re.findall(r'[一-龥ぁ-んァ-ヴー]{2,}|[a-zA-Z0-9]{2,}', title))
    return tokens - NOISE_WORDS


def extract_brand_and_product(title):
    """タイトルからブランド名と商品名を推定して抽出する。"""
    if not title:
        return "", ""
    # ブランド名パターン（カタカナ連続 or 英字）
    brands = re.findall(r'[ァ-ヴー]{3,}|[A-Z][a-zA-Z]+', title)
    brand = brands[0] if brands else ""

    # 商品名: ブランド後の最初の意味のある部分
    clean = re.sub(r'[【\[（\(＼].*?[】\]）\)／]', ' ', title)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return brand, clean


def smart_truncate(text, max_len=35):
    """日本語の区切り文字で自然に切る。途中切れを防止。"""
    if len(text) <= max_len:
        return text
    # 自然な区切りポイントを探す
    cut_chars = ['　', ' ', '/', '・', '｜', '|', '、', ',']
    best_cut = max_len
    for i in range(max_len, max(max_len - 10, 0), -1):
        if text[i] in cut_chars:
            best_cut = i
            break
    return text[:best_cut].rstrip()


def extract_tags(title, brand=""):
    """タイトルからタグを抽出。途中切れしない。"""
    tags = set()
    if brand and len(brand) >= 2:
        tags.add(brand)
    # カタカナ連続3文字以上を抽出
    katakana = re.findall(r'[ァ-ヴー]{3,}', title)
    for k in katakana:
        if k not in NOISE_WORDS and len(k) >= 3:
            tags.add(k)
    # 英字ブランド名
    eng = re.findall(r'[A-Z][a-zA-Z]{2,}', title)
    for e in eng:
        if e.lower() not in {'the', 'and', 'for', 'with'}:
            tags.add(e)
    tags.add("価格比較")
    tags.add("購入ガイド")
    return list(tags)[:6]


def calculate_ivs(item):
    """IVS (知育価値スコア) を算出。"""
    name = item.get('name', item.get('title', ''))
    features = " ".join(item.get('features', []))
    price = item.get('price', 0) or 0

    edu = 3.0
    edu_kw = ['知育', 'モンテッソーリ', 'STEM', 'プログラミング', 'ブロック',
              '思考力', '想像力', '空間把握', '学習', '教育', 'パズル']
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
        pros.append("安全性が高く安心")
    if any(k in full for k in ['長く遊べる', '成長に合わせて', '幅広い年齢']):
        pros.append("長く遊べる")
    if any(k in full for k in ['簡単', 'すぐ遊べる', 'シンプル', 'タッチ']):
        pros.append("かんたんに遊べる")
    if any(k in full for k in ['知育', '学習', '思考力', '創造力', '教育']):
        pros.append("遊びながら学べる")
    if any(k in full for k in ['グッドトイ', '受賞', '大賞']):
        pros.append("受賞歴あり！")
    if any(k in full for k in ['人気', 'ベストセラー', 'ランキング']):
        pros.append("みんなに人気")
    if not pros:
        pros = ["みんなに人気", "定番のおもちゃ"]

    cons = []
    if item.get('price', 0) > 10000:
        cons.append("お値段がちょっと高め")
    # 電池が「別売り」の場合のみ。「電池は使用しません」は除外
    has_battery_needed = any(k in full for k in ['別売り', '電池別売'])
    has_no_battery = '電池は使用しません' in full or '電池不要' in full
    if has_battery_needed and not has_no_battery:
        cons.append("電池がべつに必要だよ")
    if any(k in full for k in ['小さい', 'パーツ', '部品']):
        cons.append("小さいパーツがあるよ")
    if not cons:
        cons = ["とくになし！"]
    return pros[:3], cons[:2]


def find_best_match(target_title, pool_items, min_overlap=2):
    """固有トークンでのマッチング。min_overlap=2に緩和。"""
    if not pool_items:
        return None
    t_tokens = extract_product_tokens(target_title)
    if len(t_tokens) < 1:
        return None

    best, best_score, best_ratio = None, 0, 0.0
    for item in pool_items:
        p_tokens = extract_product_tokens(item.get("title", ""))
        overlap = t_tokens & p_tokens
        score = len(overlap)
        ratio = score / max(len(t_tokens), 1)

        if score >= min_overlap and (score > best_score or
                                     (score == best_score and ratio > best_ratio)):
            best_score = score
            best_ratio = ratio
            best = item
    return best


def find_related_videos(product_title, videos, max_results=2):
    if not videos:
        return []
    t_tokens = extract_product_tokens(product_title)
    scored = []
    for vid in videos:
        v_tokens = extract_product_tokens(vid.get("title", ""))
        score = len(t_tokens & v_tokens)
        if score >= 1:
            scored.append((score, vid))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored[:max_results]]


def find_related_news(product_title, news_items, max_results=2):
    """商品に関連するニュースのみ返す。関連なければ空。"""
    if not news_items:
        return []
    t_tokens = extract_product_tokens(product_title)
    scored = []
    for n in news_items:
        n_tokens = extract_product_tokens(n.get("title", ""))
        score = len(t_tokens & n_tokens)
        if score >= 2:
            scored.append((score, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"title": n.get("title", ""), "url": n.get("url", "")}
            for _, n in scored[:max_results]]


def make_search_url(platform, keyword):
    q = urllib.parse.quote(keyword[:30])
    if platform == "rakuten":
        return f"https://search.rakuten.co.jp/search/mall/{q}/"
    elif platform == "yahoo":
        return f"https://shopping.yahoo.co.jp/search?p={q}"
    return ""


def clean_title(text):
    """タイトルをクリーンにする。ブランド名と商品名を保持。"""
    if not text:
        return ""
    # 括弧内のノイズだけ除去（ただし商品名っぽいものは残す）
    noise = ['送料無料', 'ポイント10倍', '楽天1位', '限定', 'クーポン',
             '正規品', '公式', '最新', '予約', 'おまけ付き', 'ラッピング無料',
             'あす楽', '即納', '税込']
    for n in noise:
        text = text.replace(n, ' ')
    return " ".join(text.split()).strip()


def generate_description(item, brand, product_name):
    """商品の特徴から子供向けの説明文を生成する。"""
    features = item.get('features', [])
    price = item.get('price', 0)
    lines = []
    lines.append(f"「{product_name}」は、{brand}から発売されているおもちゃだよ！")
    if price:
        lines.append(f"お値段は **￥{price:,}** からだよ。")
    if features:
        lines.append("このおもちゃの特徴は…")
        for f in features[:3]:
            # 特徴を短く要約
            short = f[:60] + "…" if len(f) > 60 else f
            lines.append(f"- {short}")
    return "\n".join(lines)


def load_cross_search_data():
    """クロスサーチ結果をASINインデックスで読み込む。"""
    r_index, y_index = {}, {}
    r_path = pathlib.Path("data/raw/rakuten_matched.json")
    y_path = pathlib.Path("data/raw/yahoo_matched.json")
    if r_path.exists():
        try:
            for item in json.loads(r_path.read_text(encoding="utf-8")).get("items", []):
                asin = item.get("matched_asin", "")
                if asin:
                    r_index[asin] = item
        except Exception:
            pass
    if y_path.exists():
        try:
            for item in json.loads(y_path.read_text(encoding="utf-8")).get("items", []):
                asin = item.get("matched_asin", "")
                if asin:
                    y_index[asin] = item
        except Exception:
            pass
    return r_index, y_index


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

    # ASINベースのクロスサーチ結果を読み込む
    r_cross, y_cross = load_cross_search_data()
    print(f"Cross-search data: Rakuten={len(r_cross)}, Yahoo={len(y_cross)}")

    if not amazon_items:
        print("No Amazon items to process")
        return

    articles_dir = pathlib.Path("data/articles")
    existing_asins: set[str] = set()
    _ASIN_FROM_SLUG = re.compile(r"-(B0[A-Z0-9]{8})$")
    _SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")
    if articles_dir.exists():
        for f in articles_dir.glob("*.json"):
            if f.stem.endswith(_SIDECAR_SUFFIXES):
                continue
            asin_from_file = None
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                product = meta.get("product") if isinstance(meta.get("product"), dict) else None
                if product and product.get("asin"):
                    asin_from_file = product["asin"]
            except (json.JSONDecodeError, OSError):
                pass
            if not asin_from_file:
                m = _ASIN_FROM_SLUG.search(f.stem)
                if m:
                    asin_from_file = m.group(1)
            if asin_from_file:
                existing_asins.add(asin_from_file)

    today = datetime.now().strftime('%Y-%m-%d')
    generated = 0

    for item in amazon_items:
        asin = item.get("asin", "")
        if not asin:
            continue

        slug = f"{today}-{asin}"
        if asin in existing_asins:
            print(f"Skip (ASIN {asin} already has an article): {slug}")
            continue

        title_raw = item.get("title", "")
        title_clean = clean_title(title_raw)
        brand, _ = extract_brand_and_product(title_raw)
        product_name = smart_truncate(title_clean, 35)
        price_amazon = item.get("price", 0) or 0

        # --- マッチング: クロスサーチ結果を優先、なければトークンマッチ ---
        r_match = r_cross.get(asin) or find_best_match(title_raw, rakuten_items)
        y_match = y_cross.get(asin) or find_best_match(title_raw, yahoo_items)

        price_rakuten = r_match.get("price", 0) if r_match else 0
        price_yahoo = y_match.get("price", 0) if y_match else 0

        rakuten_url = r_match.get("url", "") if r_match else make_search_url("rakuten", title_clean)
        yahoo_url = y_match.get("url", "") if y_match else make_search_url("yahoo", title_clean)

        # 最安プラットフォーム判定
        prices = {}
        if price_amazon > 0: prices["Amazon"] = price_amazon
        if price_rakuten > 0: prices["楽天"] = price_rakuten
        if price_yahoo > 0: prices["Yahoo"] = price_yahoo

        if prices:
            best_platform = min(prices, key=prices.get)
            best_price = prices[best_platform]
        else:
            best_platform = "Amazon"
            best_price = price_amazon

        ivs_score, ivs_detail = calculate_ivs(item)
        pros, cons = generate_pros_cons(item)

        # 関連動画: per_asin/<ASIN>/youtube.json を優先、空ならグローバル fallback
        yt_pool = youtube_items
        per_asin_yt = pathlib.Path(f"data/raw/per_asin/{asin}/youtube.json")
        if per_asin_yt.exists():
            try:
                pa_items = json.loads(per_asin_yt.read_text(encoding="utf-8")).get("items", [])
                if pa_items:
                    yt_pool = pa_items
            except Exception:
                pass
        related_vids = find_related_videos(title_raw, yt_pool)
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

        # 関連ニュース（関連するものだけ）
        related_news = find_related_news(title_raw, news_items)

        # 関連書籍
        related_books = []
        b_match = find_best_match(title_raw, books_items, min_overlap=2)
        if b_match:
            related_books.append({
                "title": b_match.get("title", ""),
                "url": b_match.get("url", ""),
                "image": b_match.get("image", ""),
            })

        internal_links = get_related_articles(title_clean)

        # タグ生成（途中切れ防止）
        tags = extract_tags(title_raw, brand)

        # 記事タイトル（自然な切り方）
        article_title = f"【価格比較】{product_name}｜どこで買うのが一番おトク？"

        # 商品紹介文（子どもフレンドリー）
        intro_text = generate_description(item, brand or "メーカー", product_name)

        # おもちゃロボットのコメント
        if best_price > 0 and len(prices) > 1:
            robot_comment = f"おもちゃロボが調べたよ！「{product_name}」は、いま **{best_platform}** で買うのが一番おトクだよ💰 お値段は ￥{best_price:,} だよ！"
        elif best_price > 0:
            robot_comment = f"おもちゃロボが調べたよ！「{product_name}」は **Amazon** で ￥{best_price:,} で買えるよ🛒"
        else:
            robot_comment = f"おもちゃロボが「{product_name}」を調べたよ！ぜひチェックしてみてね🔍"

        article = {
            "slug": slug,
            "title": article_title,
            "meta_description": f"{product_name}をAmazon・楽天・Yahooで徹底比較！最安値・スコア・関連動画つき購入ガイド。",
            "date": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "tags": tags,
            "product": {
                "asin": asin,
                "name": product_name,
                "name_full": title_raw,
                "brand": brand,
                "image": item.get("image", ""),
                "features": item.get("features", []),
                "intro": intro_text,
                "pros": pros,
                "cons": cons,
                "ivs_score": ivs_score,
                "ivs_detail": ivs_detail,
                "prices": {
                    "amazon": {
                        "price": price_amazon,
                        "url": item.get("url", ""),
                        "availability": item.get("availability", ""),
                        "loyalty_points": item.get("loyalty_points", 0) or 0,
                        "savings_percentage": item.get("savings_percentage", 0) or 0,
                    },
                    "rakuten": {
                        "price": price_rakuten,
                        "url": rakuten_url,
                        "matched_title": r_match.get("title", "") if r_match else "",
                        "is_search": r_match is None,
                    },
                    "yahoo": {
                        "price": price_yahoo,
                        "url": yahoo_url,
                        "matched_title": y_match.get("title", "") if y_match else "",
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
            "editorial_comment": robot_comment,
        }

        articles_dir.mkdir(parents=True, exist_ok=True)
        out_path = articles_dir / f"{slug}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=4)
        print(f"Generated: {out_path}")
        existing_asins.add(asin)
        generated += 1

    print(f"\nTotal: {generated} articles generated")


if __name__ == "__main__":
    main()
