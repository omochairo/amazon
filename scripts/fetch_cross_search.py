"""
fetch_cross_search.py
Amazon商品データを読み、各商品の名前で楽天・Yahoo を個別検索して
data/raw/rakuten_matched.json, data/raw/yahoo_matched.json に保存する。

これにより、ジャンル検索では見つからない商品も正確にマッチできる。
"""

import os
import sys
import json
import re
import time
import logging
import pathlib
import requests
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cross_search")

# --- 楽天 API ---
RAKUTEN_SEARCH_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"

# --- Yahoo API ---
YAHOO_SEARCH_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
VC_REFERRAL_BASE = "https://ck.jp.ap.valuecommerce.com/servlet/referral"


def extract_search_keyword(title):
    """Amazonタイトルから検索用キーワードを抽出する。
    ブランド名 + 商品シリーズ名を中心に、短いキーワードにする。
    """
    # 括弧内を除去
    clean = re.sub(r'[【\[（\(].*?[】\]）\)]', ' ', title)
    # 著作権表記を除去
    clean = re.sub(r'\(C\).*?(?=\s|$)', '', clean)
    # ノイズ語除去
    noise = ['送料無料', 'ポイント10倍', '正規品', '公式', '最新', '予約',
             'おまけ付き', 'ラッピング無料', 'あす楽', '即納', '税込',
             '知育玩具', 'おもちゃ', 'プレゼント', '誕生日', 'ギフト']
    for n in noise:
        clean = clean.replace(n, ' ')
    # 短くまとめる（最初の意味のある3-4単語程度）
    tokens = clean.split()
    # ブランド名を含む最初の数トークンを使う
    keyword = " ".join(tokens[:4]).strip()
    if len(keyword) < 3:
        keyword = " ".join(tokens[:6]).strip()
    return keyword[:40]  # 長すぎると検索ヒットしない


def search_rakuten(keyword, app_id, access_key="", aff_id=""):
    """楽天で1商品を検索して最も関連性の高い結果を返す。"""
    # RMS API (fetch_rakuten.pyと同じエンドポイント)
    rms_url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    public_url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"

    # accessKeyがあればRMS API、なければ公開APIを使用
    if access_key:
        url = rms_url
        params = {
            "applicationId": app_id,
            "accessKey": access_key,
            "keyword": keyword,
            "sort": "-updateTimestamp",
            "formatVersion": 2,
            "hits": 3,
        }
        headers = {
            "Referer": "https://github.com/omochairo/amazon",
            "Origin": "https://github.com/omochairo/amazon"
        }
    else:
        url = public_url
        params = {
            "applicationId": app_id,
            "keyword": keyword,
            "sort": "standard",
            "formatVersion": 2,
            "hits": 3,
        }
        headers = {}

    if aff_id:
        params["affiliateId"] = aff_id

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Rakuten search failed for '{keyword}': HTTP {resp.status_code} - {resp.text[:200]}")
            return None
        data = resp.json()
        raw_items = data.get("Items", [])
        if not raw_items:
            logger.info(f"Rakuten: 0 results for '{keyword}'")
            return None

        # 最初のアイテムを返す
        item = raw_items[0]
        i = item.get("Item", item) if isinstance(item, dict) else {}

        image_url = ""
        img_list = i.get("mediumImageUrls", [])
        if isinstance(img_list, list) and img_list:
            first_img = img_list[0]
            if isinstance(first_img, dict):
                image_url = first_img.get("imageUrl", "")
            elif isinstance(first_img, str):
                image_url = first_img

        return {
            "title": i.get("itemName", ""),
            "price": i.get("itemPrice", 0),
            "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
            "image": image_url,
            "itemCode": i.get("itemCode", ""),
            "source": "Rakuten",
        }
    except Exception as e:
        logger.error(f"Rakuten search error: {e}")
        return None


def search_yahoo(keyword, client_id, sid="", pid=""):
    """Yahooで1商品を検索して最も関連性の高い結果を返す。"""
    params = {
        "appid": client_id,
        "query": keyword,
        "results": 3,
        "sort": "-score",
        "in_stock": "true",
    }
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="

    try:
        resp = requests.get(YAHOO_SEARCH_URL, params=params, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Yahoo search failed for '{keyword}': HTTP {resp.status_code}")
            return None
        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return None

        hit = hits[0]
        raw_url = hit.get("url", "")
        if "valuecommerce.com" not in raw_url and sid and pid:
            encoded = urllib.parse.quote(raw_url, safe="")
            affiliate_url = f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url={encoded}"
        else:
            affiliate_url = raw_url

        image = hit.get("image", {})
        image_url = ""
        if isinstance(image, dict):
            image_url = image.get("medium") or image.get("small") or ""

        return {
            "title": hit.get("name", ""),
            "price": hit.get("price", 0),
            "url": affiliate_url,
            "image": image_url,
            "source": "Yahoo",
        }
    except Exception as e:
        logger.error(f"Yahoo search error: {e}")
        return None


def main():
    # Amazon商品データを読む
    amazon_path = pathlib.Path("data/raw/amazon.json")
    if not amazon_path.exists():
        logger.error("data/raw/amazon.json not found")
        return

    amazon_data = json.loads(amazon_path.read_text(encoding="utf-8"))
    amazon_items = amazon_data.get("items", [])
    if not amazon_items:
        logger.warning("No Amazon items found")
        return

    # API キー
    rakuten_app_id = os.environ.get("RAKUTEN_APP_ID", "")
    rakuten_access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "")
    rakuten_aff_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
    yahoo_client_id = os.environ.get("YAHOO_CLIENT_ID", "")
    vc_sid = os.environ.get("VALUECOMMERCE_SID", "")
    vc_pid = os.environ.get("VALUECOMMERCE_PID", "")

    rakuten_results = []
    yahoo_results = []

    for item in amazon_items:
        asin = item.get("asin", "")
        title = item.get("title", "")
        keyword = extract_search_keyword(title)
        logger.info(f"Cross-searching: {keyword} (ASIN: {asin})")

        # 楽天検索
        if rakuten_app_id:
            r_result = search_rakuten(keyword, rakuten_app_id, rakuten_access_key, rakuten_aff_id)
            if r_result:
                r_result["matched_asin"] = asin
                r_result["search_keyword"] = keyword
                rakuten_results.append(r_result)
                logger.info(f"  → Rakuten: {r_result['title'][:40]}... ￥{r_result['price']}")
            else:
                logger.info(f"  → Rakuten: not found")
            time.sleep(0.5)  # Rate limit

        # Yahoo検索
        if yahoo_client_id:
            y_result = search_yahoo(keyword, yahoo_client_id, vc_sid, vc_pid)
            if y_result:
                y_result["matched_asin"] = asin
                y_result["search_keyword"] = keyword
                yahoo_results.append(y_result)
                logger.info(f"  → Yahoo: {y_result['title'][:40]}... ￥{y_result['price']}")
            else:
                logger.info(f"  → Yahoo: not found")
            time.sleep(1.0)  # Yahoo rate limit: 1 query/sec

    # 保存
    out_dir = pathlib.Path("data/raw")
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "rakuten_matched.json", "w", encoding="utf-8") as f:
        json.dump({"items": rakuten_results}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(rakuten_results)} Rakuten matches")

    with open(out_dir / "yahoo_matched.json", "w", encoding="utf-8") as f:
        json.dump({"items": yahoo_results}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(yahoo_results)} Yahoo matches")


if __name__ == "__main__":
    main()
