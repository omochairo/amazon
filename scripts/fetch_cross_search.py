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


_AGE_DIGIT_TOKEN = re.compile(r'^[0-9]{1,3}$')
_AGE_RANGE_TOKEN = re.compile(r'^[0-9]{1,2}[\-〜~～][0-9]{1,2}$')
_AGE_SUFFIX_TOKEN = re.compile(r'^([0-9]{1,2}[\-〜~～])?[0-9]{1,2}(歳|才|か月|ヶ月|カ月)(以上|から|まで|向け|[〜~～])?$')
_KANJI_DIGIT_TOKEN = re.compile(r'^[一二三四五六七八九十]$')
_MODEL_PATTERN = re.compile(r'^[A-Z][A-Z0-9\-]{3,11}$')


def _is_age_token(t):
    """年齢を表すトークンかどうか (Rakuten API が嫌う 'keyword末尾の数字' の元凶)。"""
    return bool(
        _AGE_DIGIT_TOKEN.match(t)
        or _AGE_RANGE_TOKEN.match(t)
        or _AGE_SUFFIX_TOKEN.match(t)
        or _KANJI_DIGIT_TOKEN.match(t)
    )


def _strip_trailing_digits(keyword):
    """末尾の数字トークン列を除去する (Rakuten 400 'keyword is not valid' 対策)。"""
    toks = keyword.split()
    while toks and _is_age_token(toks[-1]):
        toks.pop()
    return " ".join(toks)


def extract_search_keyword(title):
    """Amazonタイトルから検索用キーワードを抽出する。
    ブランド名 + 型番を最優先し、なければブランド名 + 商品シリーズ名にする。
    末尾が数字トークンで終わると Rakuten Ichiba が HTTP 400 を返すため、
    年齢を表す数字/数字+歳/漢数字単独トークンは除去する。
    """
    # 括弧内を除去
    clean = re.sub(r'[【\[（\(].*?[】\]）\)]', ' ', title)
    # 著作権表記を除去
    clean = re.sub(r'\(C\).*?(?=\s|$)', '', clean)
    # ノイズ語除去
    noise = ['送料無料', 'ポイント10倍', '正規品', '公式', '最新', '予約',
             'おまけ付き', 'ラッピング無料', 'あす楽', '即納', '税込',
             '知育玩具', 'おもちゃ', 'プレゼント', '誕生日', 'ギフト',
             '2個セット', '3歳から', '男の子', '女の子', '対象年齢']
    for n in noise:
        clean = clean.replace(n, ' ')

    tokens = [t for t in clean.split() if t]
    if not tokens:
        return title[:40]

    # 年齢を表すトークン (純数字 / 数字+歳 / 漢数字単独) は API が嫌うので全て除去
    tokens = [t for t in tokens if not _is_age_token(t)]
    if not tokens:
        return title[:40]

    # 型番候補: 英字で始まり、英数字混合、4〜12文字のトークン
    # 例: HCM807, EH-2310, B-901, BO-100
    models = [t for t in tokens if _MODEL_PATTERN.match(t)]

    if models:
        # ブランド名 (先頭1〜2語) + 型番1個
        brand = " ".join(tokens[:2])
        keyword = f"{brand} {models[0]}"
    else:
        # フォールバック: 最初の方の単語をまとめる
        keyword = " ".join(tokens[:4]).strip()
        if len(keyword) < 3:
            keyword = " ".join(tokens[:6]).strip()

    # 防御的: 何かの拍子に末尾へ数字が残った場合に再度トリム
    keyword = _strip_trailing_digits(keyword).strip()
    return keyword[:40] if keyword else title[:40]


def _parse_rakuten_item(raw_item):
    """楽天APIの個別のItem要素を正規化された辞書に変換する。"""
    i = raw_item.get("Item", raw_item) if isinstance(raw_item, dict) else {}
    if not i:
        return None

    image_url = ""
    img_list = i.get("mediumImageUrls", []) or i.get("largeImageUrl", [])
    if isinstance(img_list, list) and img_list:
        first_img = img_list[0]
        if isinstance(first_img, dict):
            image_url = first_img.get("imageUrl", "")
        elif isinstance(first_img, str):
            image_url = first_img
    elif isinstance(img_list, str):
        image_url = img_list

    return {
        "title": i.get("itemName", "") or i.get("title", ""),
        "price": i.get("itemPrice", 0),
        "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
        "image": image_url,
        "itemCode": i.get("itemCode", ""),
    }


def _select_median_priced_item(items):
    """価格中央値±50%の範囲外を除外し、残ったものの中で中央値に最も近いものを返す。"""
    if not items:
        return None
    prices = sorted([it.get("price", 0) for it in items if it.get("price", 0) > 0])
    if not prices:
        return items[0]
    median = prices[len(prices)//2]
    low, high = median * 0.5, median * 1.5
    in_range = [it for it in items if low <= it.get("price", 0) <= high]
    if not in_range:
        return items[0]
    return min(in_range, key=lambda x: abs(x.get("price", 0) - median))


def _fetch_rakuten_ichiba(keyword, app_id, access_key, aff_id, hits=15):
    """Ichiba を access_key の有無で RMS or 公開 API に振り分けて呼び出す。"""
    if access_key:
        url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
        params = {
            "applicationId": app_id,
            "accessKey": access_key,
            "keyword": keyword,
            "sort": "standard",
            "formatVersion": 2,
            "hits": hits,
        }
        headers = {
            "Referer": "https://github.com/omochairo/amazon",
            "Origin": "https://github.com/omochairo/amazon",
        }
    else:
        url = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
        params = {
            "applicationId": app_id,
            "keyword": keyword,
            "sort": "standard",
            "formatVersion": 2,
            "hits": hits,
        }
        headers = {}
    if aff_id:
        params["affiliateId"] = aff_id
    return requests.get(url, params=params, headers=headers, timeout=10)


def search_rakuten_tiered(keyword, app_id, access_key="", aff_id=""):
    """楽天で階層的検索を行う (Books -> Ichiba -> Shortened Ichiba)"""

    # Stage 1: Rakuten Books (公開 API のみ、accessKey 非対応)
    books_url = "https://app.rakuten.co.jp/services/api/BooksTotal/Search/20170404"
    books_params = {
        "applicationId": app_id,
        "keyword": keyword,
        "formatVersion": 2,
        "hits": 10,
    }
    if aff_id:
        books_params["affiliateId"] = aff_id

    try:
        resp = requests.get(books_url, params=books_params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            raw_items = data.get("Items", [])
            if raw_items:
                logger.info(f"Rakuten Stage1 (Books): {len(raw_items)} hits for '{keyword}'")
                parsed_items = [it for it in [_parse_rakuten_item(ri) for ri in raw_items] if it]
                best = _select_median_priced_item(parsed_items)
                if best:
                    best["source"] = "Rakuten Books"
                    return best
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"Rakuten Stage1 error: {e}")

    # Stage 2: Rakuten Ichiba (access_key あれば RMS API、なければ公開 API)
    stage2_keyword = keyword
    try:
        resp = _fetch_rakuten_ichiba(stage2_keyword, app_id, access_key, aff_id, hits=15)
        # 400 "keyword is not valid" は末尾の数字が原因なことが多いので一度だけ trim してリトライ
        if resp.status_code == 400 and "keyword" in resp.text:
            trimmed = _strip_trailing_digits(stage2_keyword).strip()
            if trimmed and trimmed != stage2_keyword:
                logger.info(f"Rakuten Stage2 retry with trimmed keyword: '{stage2_keyword}' → '{trimmed}'")
                stage2_keyword = trimmed
                resp = _fetch_rakuten_ichiba(stage2_keyword, app_id, access_key, aff_id, hits=15)
        if resp.status_code == 200:
            data = resp.json()
            raw_items = data.get("Items", [])
            if raw_items:
                api_label = "Ichiba RMS" if access_key else "Ichiba"
                logger.info(f"Rakuten Stage2 ({api_label}): {len(raw_items)} hits for '{stage2_keyword}'")
                parsed_items = [it for it in [_parse_rakuten_item(ri) for ri in raw_items] if it]
                best = _select_median_priced_item(parsed_items)
                if best:
                    best["source"] = "Rakuten"
                    return best
        else:
            logger.warning(f"Rakuten Stage2 failed for '{stage2_keyword}': HTTP {resp.status_code} - {resp.text[:200]}")
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"Rakuten Stage2 error: {e}")

    # Stage 3: Shortened Ichiba (同じく RMS or 公開 API を継承)
    tokens = keyword.split()
    if len(tokens) > 2:
        short_keyword = " ".join(tokens[:2])
        try:
            resp = _fetch_rakuten_ichiba(short_keyword, app_id, access_key, aff_id, hits=15)
            if resp.status_code == 200:
                data = resp.json()
                raw_items = data.get("Items", [])
                if raw_items:
                    logger.info(f"Rakuten Stage3 (Shortened): {len(raw_items)} hits for '{short_keyword}'")
                    parsed_items = [it for it in [_parse_rakuten_item(ri) for ri in raw_items] if it]
                    best = _select_median_priced_item(parsed_items)
                    if best:
                        best["source"] = "Rakuten"
                        return best
        except Exception as e:
            logger.error(f"Rakuten Stage3 error: {e}")

    return None


def _yahoo_query(keyword, client_id, sid="", pid=""):
    """Yahoo Shopping API を 1 回叩いて、parsed_items のリストを返す。"""
    params = {
        "appid": client_id,
        "query": keyword,
        "results": 15,
        "sort": "-score",
        "in_stock": "true",
    }
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="

    resp = requests.get(YAHOO_SEARCH_URL, params=params, timeout=10)
    if resp.status_code != 200:
        logger.warning(f"Yahoo search failed for '{keyword}': HTTP {resp.status_code}")
        return []
    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        return []

    parsed_items = []
    for hit in hits:
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

        parsed_items.append({
            "title": hit.get("name", ""),
            "price": hit.get("price", 0),
            "url": affiliate_url,
            "image": image_url,
            "source": "Yahoo",
        })
    return parsed_items


def search_yahoo(keyword, client_id, sid="", pid=""):
    """Yahooで階層的に検索する (full keyword → 先頭2語 → 先頭1語)。"""
    tokens = keyword.split()
    candidates = [keyword]
    if len(tokens) > 2:
        candidates.append(" ".join(tokens[:2]))
    if len(tokens) > 1:
        candidates.append(tokens[0])
    # 重複除去 (順序保持)
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for idx, kw in enumerate(candidates, start=1):
        try:
            items = _yahoo_query(kw, client_id, sid, pid)
        except Exception as e:
            logger.error(f"Yahoo Stage{idx} error for '{kw}': {e}")
            items = []
        if items:
            logger.info(f"Yahoo Stage{idx}: {len(items)} hits for '{kw}'")
            best = _select_median_priced_item(items)
            if best:
                return best
        if idx < len(candidates):
            time.sleep(1.0)  # Yahoo rate limit: 1 query/sec
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
            r_result = search_rakuten_tiered(keyword, rakuten_app_id, rakuten_access_key, rakuten_aff_id)
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
