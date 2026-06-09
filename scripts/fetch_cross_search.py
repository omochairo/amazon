"""
fetch_cross_search.py
Amazon商品データを読み、各商品の名前で楽天・Yahoo を個別検索して
data/raw/rakuten_matched.json, data/raw/yahoo_matched.json に保存する。

これにより、ジャンル検索では見つからない商品も正確にマッチできる。

実行モード:
  通常: python scripts/fetch_cross_search.py
    → amazon.json は force_refresh, articles のみの ASIN は欠落のみ補填
  低信頼再検索: python scripts/fetch_cross_search.py --re-search-low-confidence
    → JAN が利用可能で、既存マッチが (a) 欠落 / (b) quality 不合格 /
      (c) text マッチ (JAN アップグレード余地) のいずれかに該当する
      ASIN を強制再検索。新結果が品質ガード不合格で旧結果が合格なら旧を保持。
"""

import os
import sys
import json
import re
import time
import logging
import pathlib
import argparse
import requests
import urllib.parse
import collections
from datetime import datetime, timezone

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
# 型番: 英大文字+数字を**両方**含む 4〜12 文字。BEYBLADE / TOMICA / T-SPARK 等の
# 単語シリーズ名 (英字のみ) を誤検出しないように数字を必須にする。
_MODEL_PATTERN = re.compile(r'^(?=[A-Z0-9\-]{4,12}$)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9\-]+$')
# Issue #1087 Phase 2: タイトル中盤の固有商品名 (e.g. メロディーゴーラウンド,
# クラシックドラム) を product identifier として優先採用するための判定。長め (8 文字
# 以上) の純カタカナ token は generic 修飾語ではなく商品/シリーズ名である確度が高い。
_KATAKANA_PRODUCT_RE = re.compile(r'^[゠-ヿー・]+$')
_KATAKANA_PRODUCT_MIN_LEN = 8
# Issue #1087 Phase 2: noise 除去で「以上 / 以下 / 向け / から」等の age 修飾語が
# 末尾に残ると kw 末尾が無意味なものになり、検索 noise を増やすので token として除去。
_STOP_TOKENS = frozenset({
    '以上', '以下', '前後', '向け', 'から', 'まで', '対応',
    '入り', '入', '版', '用',
})
# Rakuten Stage3 で短縮した結果これだけになる場合、汎用過ぎてマッチが事故るため
# 短縮検索を抑止するブランド/シリーズ語
_GENERIC_BRAND_TOKENS = {
    'タカラトミー', 'TAKARA', 'TOMY', 'バンダイ', 'BANDAI', 'エポック',
    'ボーネルンド', 'EPOCH', 'KONAMI', 'コナミ', 'セガ', 'SEGA',
    'トミカ', 'TOMICA', 'プラレール', 'BEYBLADE', 'ベイブレード',
    'ポケモン', 'ポケットモンスター', 'ディズニー', 'DISNEY',
    'おもちゃ', '知育玩具',
}


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


def _dedupe_tokens(tokens):
    """順序保持のまま、すでに出現済みの同一トークンを除去する。"""
    seen = set()
    out = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def extract_search_keyword(title):
    """Amazonタイトルから検索用キーワードを抽出する。
    ブランド名 + 型番を最優先し、なければブランド名 + 商品シリーズ名にする。
    末尾が数字トークンで終わると Rakuten Ichiba が HTTP 400 を返すため、
    年齢を表す数字/数字+歳/漢数字単独トークンは除去する。

    Issue #1087 Phase 2 改善:
    - noise 除去を「トークン化後の完全一致」に変更 (旧 substring 置換は
      "木製おもちゃのだいわ" のような noise 内包ブランドを破壊していた)。
    - 型番が見つからないとき、タイトル中盤の長い純カタカナ token (8 文字以上)
      を product identifier として優先採用 (例: メロディーゴーラウンド,
      クラシックドラム, レインボーアバカス)。
    - 末尾 "以上"/"以下"/"向け" 等の age 修飾語 token を除去。
    """
    # 1. 記号 ! と ！ やその他装飾記号をスペースに置換 (ノイズ記号のクリーンアップ)
    clean = re.sub(r'[!！?？♪]', ' ', title)

    # 2. 丸括弧 / 角括弧 / 隅つき括弧の中身は除去 (TAKARA TOMY 等の重複表記やメタ情報用)
    clean = re.sub(r'[【\[（\(].*?[】\]）\)]', ' ', clean)
    # 著作権表記を除去
    clean = re.sub(r'\(C\).*?(?=\s|$)', '', clean)
    # カギ括弧 / 二重カギ / 山括弧 は記号のみスペースに置換し、中身は保持する。
    # (商品名そのものを 『…』 で囲むタカラトミー系タイトルで固有名が落ちないようにする)
    clean = re.sub(r'[「」『』〈〉《》]', ' ', clean)
    # 単独で残った括弧記号もスペースに置換
    clean = re.sub(r'[【】()（）\[\]]', ' ', clean)

    # 3. サイズ・重量・数量などの表記を除去
    # - 連結サイズ: 15x15cm, 75×87×14cm, 30x130x210mm, 26×38 など
    clean = re.sub(r'\d+(?:\.\d+)?\s*[x×*]\s*\d+(?:\.\d+)?(?:\s*[x×*]\s*\d+(?:\.\d+)?)?\s*(?:cm|mm|m|inch|インチ)?', ' ', clean, flags=re.IGNORECASE)
    # - 単位付き数値: 15cm, 1.5kg, 500g, 2.4mm, 100ml, 1000ピース, 90pcs, 10個, 24色 など
    clean = re.sub(r'\d+(?:\.\d+)?\s*(?:cm|mm|m|kg|g|ml|l|pcs|ピース|個|色|枚|倍|pt|L|才|歳)(?=\s|$|[^a-zA-Z0-9])', ' ', clean, flags=re.IGNORECASE)

    # 4a. substring で剥がしても誤爆しない short Latin / 記号系 noise は string-replace で除去
    sub_noise = ['送料無料', 'ポイント10倍', '正規品', '公式', '最新', '予約',
                 'おまけ付き', 'ラッピング無料', 'あす楽', '即納', '税込',
                 '2個セット', '3歳から', '対象年齢',
                 # Issue #1087 Phase 2: マーケ/受賞修飾語。B001A29UQW で
                 # 'グッドデザイン賞 グッド・トイ賞 受賞' の 3 語が tokens[:4] を埋めて
                 # 商品名 'アースカイト' を押し出す問題を緩和。
                 'グッドデザイン賞', 'グッド・トイ賞', 'グッドトイ賞']
    for n in sub_noise:
        clean = clean.replace(n, ' ')

    tokens = [t for t in clean.split() if t]
    if not tokens:
        return title[:40]

    # 4b. トークン化後に「完全一致」だけ落とす noise (おもちゃ / 知育玩具 等)。
    #     旧来の substring 除去は "木製おもちゃのだいわ" や "木のおもちゃ" のブランド
    #     /シリーズ語内に紛れた noise を意図せず破壊していた (Issue #1087 Phase 2)。
    token_noise = frozenset({
        '知育玩具', 'おもちゃ', 'プレゼント', '誕生日', 'ギフト',
        '男の子', '女の子', '子供', '子ども', '幼児', '赤ちゃん',
        # Issue #1087 Phase 2: 受賞/人気フラグも brand+受賞 で kw が埋まる原因
        '受賞', '人気',
    })
    tokens = [t for t in tokens if t not in token_noise]
    if not tokens:
        return title[:40]

    # 年齢を表すトークン (純数字 / 数字+歳 / 漢数字単独) は API が嫌うので全て除去
    tokens = [t for t in tokens if not _is_age_token(t)]
    # 末尾装飾語 (以上/以下/向け) を除去
    tokens = [t for t in tokens if t not in _STOP_TOKENS]
    if not tokens:
        return title[:40]

    # 型番候補: 英大文字+数字を両方含む 4〜12 文字 (例: HCM807, EH-2310, CX-13, CT-P09)
    models = [t for t in tokens if _MODEL_PATTERN.match(t)]

    if models:
        # ブランド名 (先頭1〜2語) + 型番1個。同一トークンの重複は除去
        head = tokens[:2]
        keyword_tokens = _dedupe_tokens(head + [models[0]])
        keyword = " ".join(keyword_tokens)
    else:
        # 型番なし: タイトル中盤の長い純カタカナ token を product identifier として優先。
        # Issue #1087 Phase 2: ブランド head + メロディーゴーラウンド のような商品名で
        # B08DCHWRJC タイプの「楽器/音色 等の generic 語に流れて別商品マッチ」を回避。
        head = tokens[:2]
        katakana_long = [
            t for t in tokens[2:]
            if _KATAKANA_PRODUCT_RE.match(t) and len(t) >= _KATAKANA_PRODUCT_MIN_LEN
        ]
        if katakana_long:
            keyword_tokens = _dedupe_tokens(head + [katakana_long[0]])
        else:
            # フォールバック: 最初の方の単語をまとめる
            keyword_tokens = _dedupe_tokens(tokens[:4])
        keyword = " ".join(keyword_tokens).strip()
        if len(keyword) < 3:
            keyword = " ".join(_dedupe_tokens(tokens[:6])).strip()

    # 防御的: 何かの拍子に末尾へ数字が残った場合に再度トリム
    keyword = _strip_trailing_digits(keyword).strip()
    return keyword[:40] if keyword else title[:40]


def _parse_rakuten_item(raw_item, is_books=False):
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

    # 在庫状況 (availability)
    availability_val = i.get("availability")
    availability = "instock"
    if is_books:
        if availability_val is not None:
            av_str = str(availability_val).strip()
            if av_str in ("6", "9"):
                availability = "outofstock"
            elif av_str in ("1", "2", "3", "4", "5", "7", "8"):
                availability = "instock"
    else:
        if availability_val is not None:
            try:
                if int(availability_val) == 0:
                    availability = "outofstock"
            except (ValueError, TypeError):
                pass

    # 送料 (free_shipping)
    if is_books:
        free_shipping = True
    else:
        postage_flag = i.get("postageFlag")
        if postage_flag is not None:
            try:
                free_shipping = (int(postage_flag) == 0)
            except (ValueError, TypeError):
                free_shipping = False
        else:
            free_shipping = False

    # あす楽 (asuraku)
    if is_books:
        asuraku = False
    else:
        asuraku_flag = i.get("asurakuFlag")
        if asuraku_flag is not None:
            try:
                asuraku = (int(asuraku_flag) == 1)
            except (ValueError, TypeError):
                asuraku = False
        else:
            asuraku = False

    return {
        "title": i.get("itemName", "") or i.get("title", ""),
        "price": i.get("itemPrice", 0),
        "url": i.get("affiliateUrl") or i.get("itemUrl", ""),
        "image": image_url,
        "itemCode": i.get("itemCode", ""),
        "availability": availability,
        "free_shipping": free_shipping,
        "asuraku": asuraku,
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


# --- prevent-side mismatch filter (#2186 P1) -------------------------------
# 中古 / ジャンク / 訳あり 等のリセラー出品はタイトルでほぼ自己申告するので、
# テキストマッチ候補から除外して check_no_reseller_pricing の偽陽性 (分岐1・2)
# を上流で消す。半角/全角の表記ゆれを吸収するため lower + NFKC 相当の素引き。
_USED_LISTING_RE = re.compile(
    r"(中古|ジャンク|訳あり|わけあり|リユース|リファービッシュ|"
    r"再生品|アウトレット|展示品|開封済|used\b|refurb)",
    re.IGNORECASE,
)

# 上流マッチャの drop 内訳カウンタ ([[feedback-omochairo-reseller-drop-overfilter]]:
# over-filter で unique 大量 drop した教訓を踏まえ、理由別件数を必ず残す)。
_DROP_STATS: "collections.Counter[str]" = collections.Counter()


def _is_used_listing(title: str) -> bool:
    """タイトルが中古/ジャンク等のリセラー出品を自己申告しているか。"""
    return bool(title) and bool(_USED_LISTING_RE.search(title))


def _extract_model_tokens(title: str) -> set[str]:
    """タイトルから型番 token (英大文字+数字 4〜12 文字) を抽出する。

    `-` 入り型番 (EH-2310) は記号差を吸収するため除去版も併せて返す。
    """
    if not title:
        return set()
    tokens: set[str] = set()
    for raw in re.split(r"[\s/／・,，、（）()\[\]【】]+", title.upper()):
        raw = raw.strip()
        if _MODEL_PATTERN.match(raw):
            tokens.add(raw)
            if "-" in raw:
                tokens.add(raw.replace("-", ""))
    return tokens


def _title_model_consistent(amazon_title: str, candidate_title: str) -> bool:
    """Amazon 側に型番があるとき、候補タイトルが同じ型番を含むか。

    型番が無い商品 (純カタカナ名のみ等) では判定不能なので True を返し、
    over-filter を避ける (型番ありの誤マッチ = 書籍/別商品 だけを高精度に弾く)。
    """
    amz_models = _extract_model_tokens(amazon_title)
    if not amz_models:
        return True
    cand_norm = (candidate_title or "").upper().replace("-", "")
    return any(m.replace("-", "") in cand_norm for m in amz_models)


def _filter_text_candidates(items, amazon_title, source, asin):
    """テキストマッチ候補から中古出品・型番不一致を除外する (#2186 P1 prevent)。

    JAN 直引きと違いテキスト検索は別商品/中古を拾いやすいので、中央値選択の前に
    かける。drop は理由別に `_DROP_STATS` へ計上し、各件 logger.info で残す。
    """
    kept = []
    for it in items or []:
        ctitle = it.get("title", "") or ""
        if _is_used_listing(ctitle):
            _DROP_STATS[f"{source}:used_listing"] += 1
            logger.info(f"  ✕ drop [used] {source} {asin}: {ctitle[:50]}")
            continue
        if not _title_model_consistent(amazon_title, ctitle):
            _DROP_STATS[f"{source}:model_mismatch"] += 1
            logger.info(f"  ✕ drop [model] {source} {asin}: {ctitle[:50]}")
            continue
        kept.append(it)
    return kept


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


def _rakuten_books_by_isbnjan(jan_code, app_id, aff_id):
    """Rakuten Books の `isbnjan` パラメータで JAN/EAN/ISBN を直引きする。

    商品が Books カテゴリに存在する場合、これがユニーク識別での最高精度マッチ。
    keyword 検索とは別 endpoint パラメータなので、ヒットすればテキスト検索を
    完全にスキップできる。
    """
    url = "https://app.rakuten.co.jp/services/api/BooksTotal/Search/20170404"
    params = {
        "applicationId": app_id,
        "isbnjan": jan_code,
        "formatVersion": 2,
        "hits": 5,
    }
    if aff_id:
        params["affiliateId"] = aff_id
    return requests.get(url, params=params, timeout=10)


def _classify_http_status(status_code: int) -> str:
    """JAN stage の HTTP status を `_jan_attempt` ラベルに正規化する。"""
    if status_code == 400:
        return "jan_400"
    if 500 <= status_code < 600:
        return "jan_5xx"
    return f"jan_http_{status_code}"


def search_rakuten_tiered(keyword, app_id, access_key="", aff_id="", jan_code="", amazon_title="", asin=""):
    """楽天で階層的検索を行う (JAN優先 → Books → Ichiba → Shortened Ichiba)

    `jan_code` が与えられた場合、まず Books の `isbnjan` 直引き、続いて Ichiba を
    `keyword=<JAN>` で叩く。どちらも 1 件目を即返却して `_select_median_priced_item`
    の中央値選択をバイパスする (JAN ヒット = 同一商品なので中央値選択は無意味)。
    返り値 dict には診断用に `_match_method` と `_jan_attempt` を埋め込む。
    `_jan_attempt` の値: jan_books / jan_ichiba (hit) / jan_zero / jan_400 / jan_5xx /
    jan_http_<code> / jan_error / no_jan (Issue #1087 Phase 1 observability)。
    """

    # JAN フェーズの最終 outcome を tracking (manifest/log 用)
    jan_attempt = "no_jan" if not jan_code else "jan_skipped"

    # Stage 0a: JAN 直引き (Rakuten Books `isbnjan`)
    if jan_code:
        try:
            resp = _rakuten_books_by_isbnjan(jan_code, app_id, aff_id)
            if resp.status_code == 200:
                data = resp.json()
                raw_items = data.get("Items", [])
                parsed_items = [it for it in [_parse_rakuten_item(ri, is_books=True) for ri in raw_items] if it]
                parsed_items = _filter_text_candidates(parsed_items, "", "rakuten", asin)
                if parsed_items:
                    logger.info(f"Rakuten Stage0a (Books isbnjan={jan_code}): {len(parsed_items)} hit(s)")
                    best = parsed_items[0]
                    best["source"] = "Rakuten Books"
                    best["_match_method"] = "jan_books"
                    best["_jan_attempt"] = "jan_books"
                    return best
                else:
                    jan_attempt = "jan_zero"
                    logger.info(f"Rakuten Stage0a (Books isbnjan={jan_code}): 0 hits")
            else:
                jan_attempt = _classify_http_status(resp.status_code)
                logger.warning(
                    f"Rakuten Stage0a failed (Books isbnjan={jan_code}): "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )
            time.sleep(0.5)
        except Exception as e:
            jan_attempt = "jan_error"
            logger.error(f"Rakuten Stage0a error: {e}")

        # Stage 0b: JAN を Ichiba の keyword に投入
        try:
            resp = _fetch_rakuten_ichiba(jan_code, app_id, access_key, aff_id, hits=10)
            if resp.status_code == 200:
                data = resp.json()
                raw_items = data.get("Items", [])
                parsed_items = [it for it in [_parse_rakuten_item(ri, is_books=False) for ri in raw_items] if it]
                parsed_items = _filter_text_candidates(parsed_items, "", "rakuten", asin)
                if parsed_items:
                    api_label = "Ichiba RMS" if access_key else "Ichiba"
                    logger.info(f"Rakuten Stage0b ({api_label} keyword=JAN:{jan_code}): {len(parsed_items)} hit(s)")
                    best = parsed_items[0]
                    best["source"] = "Rakuten"
                    best["_match_method"] = "jan_ichiba"
                    best["_jan_attempt"] = "jan_ichiba"
                    return best
                else:
                    jan_attempt = "jan_zero"
                    logger.info(f"Rakuten Stage0b (keyword=JAN:{jan_code}): 0 hits")
            else:
                jan_attempt = _classify_http_status(resp.status_code)
                logger.warning(f"Rakuten Stage0b failed (keyword=JAN:{jan_code}): HTTP {resp.status_code} {resp.text[:200]}")
            time.sleep(0.5)
        except Exception as e:
            jan_attempt = "jan_error"
            logger.error(f"Rakuten Stage0b error: {e}")

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
                parsed_items = [it for it in [_parse_rakuten_item(ri, is_books=True) for ri in raw_items] if it]
                parsed_items = _filter_text_candidates(parsed_items, amazon_title, "rakuten", asin)
                best = _select_median_priced_item(parsed_items)
                if best:
                    best["source"] = "Rakuten Books"
                    best["_match_method"] = "text"
                    best["_jan_attempt"] = jan_attempt
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
                parsed_items = [it for it in [_parse_rakuten_item(ri, is_books=False) for ri in raw_items] if it]
                parsed_items = _filter_text_candidates(parsed_items, amazon_title, "rakuten", asin)
                best = _select_median_priced_item(parsed_items)
                if best:
                    best["source"] = "Rakuten"
                    best["_match_method"] = "text"
                    best["_jan_attempt"] = jan_attempt
                    return best
        else:
            logger.warning(f"Rakuten Stage2 failed for '{stage2_keyword}': HTTP {resp.status_code} - {resp.text[:200]}")
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"Rakuten Stage2 error: {e}")

    # Stage 3: Shortened Ichiba (同じく RMS or 公開 API を継承)
    # ブランド+メガジャンル (例: "タカラトミー トミカ") のような汎用2語にしかならない場合、
    # 検索結果が無関係な人気商品で埋まり誤マッチを生むのでスキップ。
    tokens = keyword.split()
    if len(tokens) > 2:
        short_keyword = " ".join(tokens[:2])
        short_tokens = short_keyword.split()
        is_all_generic = short_tokens and all(t in _GENERIC_BRAND_TOKENS for t in short_tokens)
        if is_all_generic:
            logger.info(f"Rakuten Stage3 skipped (too generic): '{short_keyword}'")
        else:
            try:
                resp = _fetch_rakuten_ichiba(short_keyword, app_id, access_key, aff_id, hits=15)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_items = data.get("Items", [])
                    if raw_items:
                        logger.info(f"Rakuten Stage3 (Shortened): {len(raw_items)} hits for '{short_keyword}'")
                        parsed_items = [it for it in [_parse_rakuten_item(ri, is_books=False) for ri in raw_items] if it]
                        parsed_items = _filter_text_candidates(parsed_items, amazon_title, "rakuten", asin)
                        best = _select_median_priced_item(parsed_items)
                        if best:
                            best["source"] = "Rakuten"
                            best["_match_method"] = "text"
                            best["_jan_attempt"] = jan_attempt
                            return best
            except Exception as e:
                logger.error(f"Rakuten Stage3 error: {e}")

    return None


def _yahoo_query(keyword, client_id, sid="", pid="", jan_code="", out_status=None):
    """Yahoo Shopping API V3 itemSearch を 1 回叩いて parsed_items を返す。

    `jan_code` が指定された場合は `jan_code` パラメータで送信し、`query` は
    省略する (V3 仕様: 完全一致検索)。それ以外は従来通り `query` でテキスト検索。

    `in_stock=true` フィルタは text search のみに適用する。JAN は unique ID
    なので在庫切れでも商品ページ自体は valid (ユーザーが裏で入荷確認できる)。
    かつ小規模 Yahoo Store はしばしば inStock メタを False で送ってくるため、
    JAN 検索でこのフィルタを掛けると Yahoo Shopping カタログに実在する商品でも
    0 hits で silent fall through する (Issue #1087 続き)。Rakuten 側に同等
    フィルタが無く JAN hit 率が高い (93.5% vs Yahoo 87.2%) ことの主因と判明。
    parsed_items 側で `inStock` field を見て availability=outofstock を立て
    続けるので、availability 情報は失わない。

    `out_status` (list, optional): 渡された場合 HTTP status code を 1 要素 append
    する。例外時は append されないので caller は `len(out_status) == 0` で
    network/parse error と区別できる (Issue #1087 Phase 1 observability)。
    """
    params = {
        "appid": client_id,
        "results": 15,
        "sort": "-score",
    }
    if jan_code:
        params["jan_code"] = jan_code
    else:
        params["query"] = keyword
        params["in_stock"] = "true"
    if sid and pid:
        params["affiliate_type"] = "vc"
        params["affiliate_id"] = f"{VC_REFERRAL_BASE}?sid={sid}&pid={pid}&vc_url="

    resp = requests.get(YAHOO_SEARCH_URL, params=params, timeout=10)
    if out_status is not None:
        out_status.append(resp.status_code)
    if resp.status_code != 200:
        probe = f"JAN:{jan_code}" if jan_code else keyword
        logger.warning(f"Yahoo search failed for '{probe}': HTTP {resp.status_code}")
        return []
    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        # 0 hits も明示的に log してフォールスルー診断を可能にする。
        probe = f"JAN:{jan_code}" if jan_code else keyword
        logger.info(f"Yahoo search returned 0 hits for '{probe}'")
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

        # 在庫状況 (inStock)
        instock_val = hit.get("inStock")
        availability = "instock"
        if instock_val is False:
            availability = "outofstock"

        # 送料条件 (shipping.code)
        # code: 1 -> 設定なし (有料), code: 2 -> 送料無料, code: 3 -> 条件付き送料無料
        # ユーザー指示により、条件付き送料無料(code:3)は非表示にするため、code == 2 の場合のみ free_shipping = True とする
        shipping_code = hit.get("shipping", {}).get("code")
        free_shipping = (shipping_code == 2)

        parsed_items.append({
            "title": hit.get("name", ""),
            "price": hit.get("price", 0),
            "url": affiliate_url,
            "image": image_url,
            "source": "Yahoo",
            "availability": availability,
            "free_shipping": free_shipping,
            "asuraku": False,
        })
    return parsed_items


def search_yahoo(keyword, client_id, sid="", pid="", jan_code="", amazon_title="", asin=""):
    """Yahooで階層的に検索する (JAN優先 → full keyword → 先頭2語 → 先頭1語)。

    `jan_code` が与えられた場合、V3 itemSearch の `jan_code` パラメータで完全一致を
    最優先で試す。ヒットすればテキスト検索はスキップ (中央値選択もバイパスして即返却)。
    短縮候補が `_GENERIC_BRAND_TOKENS` のみで構成される場合 (例: 'タカラトミー トミカ',
    'タカラトミー' 単独) は無関係な人気商品にマッチするのでスキップする。
    """
    jan_attempt = "no_jan" if not jan_code else "jan_skipped"
    if jan_code:
        status_out: list[int] = []
        try:
            items = _yahoo_query("", client_id, sid, pid, jan_code=jan_code, out_status=status_out)
        except Exception as e:
            logger.error(f"Yahoo JAN error for '{jan_code}': {e}")
            items = []
        items = _filter_text_candidates(items, "", "yahoo", asin)
        if items:
            logger.info(f"Yahoo Stage0 (jan_code={jan_code}): {len(items)} hits")
            best = items[0]
            best["_match_method"] = "jan"
            best["_jan_attempt"] = "jan"
            return best
        # JAN miss の原因を分類
        if not status_out:
            jan_attempt = "jan_error"
        elif status_out[0] != 200:
            jan_attempt = _classify_http_status(status_out[0])
        else:
            jan_attempt = "jan_zero"
        time.sleep(1.0)  # Yahoo rate limit: 1 query/sec

    tokens = keyword.split()
    candidates = [keyword]
    if len(tokens) > 2:
        candidates.append(" ".join(tokens[:2]))
    if len(tokens) > 1:
        candidates.append(tokens[0])
    # 重複除去 (順序保持)
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]
    # 汎用ブランド/メガジャンル単独に短縮されたものは除外
    def _all_generic(kw):
        toks = kw.split()
        return toks and all(t in _GENERIC_BRAND_TOKENS for t in toks)
    candidates = [c for c in candidates if not (c != keyword and _all_generic(c))]

    for idx, kw in enumerate(candidates, start=1):
        try:
            items = _yahoo_query(kw, client_id, sid, pid)
        except Exception as e:
            logger.error(f"Yahoo Stage{idx} error for '{kw}': {e}")
            items = []
        items = _filter_text_candidates(items, amazon_title, "yahoo", asin)
        if items:
            logger.info(f"Yahoo Stage{idx}: {len(items)} hits for '{kw}'")
            best = _select_median_priced_item(items)
            if best:
                best["_match_method"] = "text"
                best["_jan_attempt"] = jan_attempt
                return best
        if idx < len(candidates):
            time.sleep(1.0)  # Yahoo rate limit: 1 query/sec
    return None


_ASIN_FROM_FILENAME = re.compile(r"(B0[A-Z0-9]{8}|[0-9]{9}[0-9X])")
_SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")


def _load_existing_matched(path: pathlib.Path) -> dict:
    """既存 matched JSON を {asin: item} で返す (累積マージ用)。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    index = {}
    for it in data.get("items", []):
        asin = it.get("matched_asin", "")
        if asin:
            index[asin] = it
    return index


def _load_jan_from_per_asin(per_asin_root: pathlib.Path, asin: str) -> str:
    """data/raw/per_asin/<ASIN>/amazon.json snapshot から jan_code を引く。

    amazon.json 本体に jan_code が無い articles-only ASIN のための fallback。
    snapshot 自体が古く JAN フィールドを持っていない場合は空文字を返す
    (= JAN unavailable → text フォールバックに自動で落ちる)。
    """
    snap_path = per_asin_root / asin / "amazon.json"
    if not snap_path.exists():
        return ""
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    item = snap.get("item") if isinstance(snap, dict) else None
    if isinstance(item, dict):
        jc = item.get("jan_code", "")
        if isinstance(jc, str):
            return jc.strip()
    return ""


def _load_amazon_price_from_per_asin(per_asin_root: pathlib.Path, asin: str) -> int:
    """data/raw/per_asin/<ASIN>/amazon.json snapshot から price を引く。

    --re-search-low-confidence モードで quality gate (0.5x〜2.0x 価格帯) を
    判定するために必要。snapshot が無い・price が無い場合は 0 を返す
    (= 価格制約なし → token overlap のみで判定)。
    """
    snap_path = per_asin_root / asin / "amazon.json"
    if not snap_path.exists():
        return 0
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    item = snap.get("item") if isinstance(snap, dict) else None
    if isinstance(item, dict):
        try:
            return int(item.get("price") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _is_low_confidence_match(
    existing_entry: dict | None,
    jan_code: str,
    amazon_price: int,
    quality_check_fn,
) -> tuple[bool, str]:
    """既存マッチが「再検索すべき低信頼」かを判定。

    Returns:
        (need_refresh: bool, reason: str)

    判定基準:
        - existing_entry なし → ("missing", 再検索)
        - quality_check_fn が False → ("quality_fail", 再検索)
        - JAN あり かつ _match_method == "text" → ("text_upgrade", 再検索)
        - それ以外 → (False, 保持)
    """
    if not existing_entry:
        return True, "missing"
    try:
        if not quality_check_fn(existing_entry, amazon_price):
            price = existing_entry.get("price", "?")
            return True, f"quality_fail (price={price} vs amazon={amazon_price})"
    except Exception as e:
        return True, f"quality_check_error ({e})"
    method = existing_entry.get("_match_method", "text")
    if jan_code and method == "text":
        return True, "text_upgrade"
    return False, ""


def _collect_targets(amazon_items: list, articles_dir: pathlib.Path, per_asin_root: pathlib.Path | None = None) -> dict:
    """ASIN -> (title, force_refresh, jan_code) の処理対象を集める。
    1) amazon.json (今回の検索結果) は **強制再取得** = 価格を新鮮に保つ
    2) data/articles/ にある記事の ASIN は、既存 matched に登録済みなら preserve、
       未登録なら新規検索 (1回だけ実施で逐次キャッチアップ)
    JAN コードは amazon.json から直接取得、無ければ per_asin snapshot から fallback。
    """
    targets: dict[str, tuple[str, bool, str]] = {}
    for amz in amazon_items:
        asin = amz.get("asin", "")
        if asin:
            jan = amz.get("jan_code") or ""
            if not jan and per_asin_root is not None:
                jan = _load_jan_from_per_asin(per_asin_root, asin)
            targets[asin] = (amz.get("title", ""), True, jan)

    if not articles_dir.exists():
        return targets

    for art_path in articles_dir.glob("*.json"):
        if art_path.stem.endswith(_SIDECAR_SUFFIXES):
            continue
        m = _ASIN_FROM_FILENAME.search(art_path.stem)
        if not m:
            continue
        asin = m.group(1)
        if asin in targets:
            continue
        try:
            art = json.loads(art_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = ""
        if isinstance(art.get("product"), dict):
            title = art["product"].get("name_full") or art["product"].get("name") or ""
        if not title:
            title = art.get("title", "")
        if title:
            jan = _load_jan_from_per_asin(per_asin_root, asin) if per_asin_root is not None else ""
            targets[asin] = (title, False, jan)
    return targets


def main():
    parser = argparse.ArgumentParser(description="楽天/Yahoo を Amazon ASIN 単位で cross-search")
    parser.add_argument(
        "--re-search-low-confidence",
        action="store_true",
        help=(
            "JAN コードが利用可能で既存マッチが (a) 欠落 / (b) quality 不合格 / "
            "(c) text マッチで JAN アップグレード余地 のいずれかに該当する ASIN を強制再検索。"
            "新結果が quality 不合格で旧結果が合格なら旧を保持。"
        ),
    )
    args = parser.parse_args()
    re_search_mode = args.re_search_low_confidence

    # 低信頼再検索モードでは build_post の quality gate を借用
    quality_check_fn = None
    if re_search_mode:
        try:
            from build_post import _matched_passes_quality as quality_check_fn  # noqa: F401
            logger.info("Mode: --re-search-low-confidence (using build_post._matched_passes_quality)")
        except ImportError as e:
            logger.error(f"Failed to import build_post._matched_passes_quality: {e}")
            sys.exit(1)

    # Amazon商品データを読む
    amazon_path = pathlib.Path("data/raw/amazon.json")
    if not amazon_path.exists():
        logger.error("data/raw/amazon.json not found")
        return

    amazon_data = json.loads(amazon_path.read_text(encoding="utf-8"))
    amazon_items = amazon_data.get("items", [])
    if not amazon_items:
        logger.warning("No Amazon items found")

    # 既存 matched を累積マージのベースとして読み込む
    out_dir = pathlib.Path("data/raw")
    r_existing = _load_existing_matched(out_dir / "rakuten_matched.json")
    y_existing = _load_existing_matched(out_dir / "yahoo_matched.json")

    # 処理対象を amazon.json ∪ data/articles/ で構築
    articles_dir = pathlib.Path("data/articles")
    per_asin_root = out_dir / "per_asin"
    targets = _collect_targets(amazon_items, articles_dir, per_asin_root)
    jan_available = sum(1 for (_t, _f, jan) in targets.values() if jan)
    logger.info(
        f"Cross-search targets: amazon.json={len(amazon_items)}, "
        f"articles_only={len(targets) - len(amazon_items)} "
        f"(existing rakuten={len(r_existing)}, yahoo={len(y_existing)}, "
        f"jan_available={jan_available}/{len(targets)})"
    )

    # amazon.json 内の price をルックアップテーブル化 (per-target で fallback 用)
    amazon_price_by_asin: dict[str, int] = {}
    for amz in amazon_items:
        asin = amz.get("asin", "")
        if not asin:
            continue
        try:
            amazon_price_by_asin[asin] = int(amz.get("price") or 0)
        except (TypeError, ValueError):
            amazon_price_by_asin[asin] = 0

    # API キー
    rakuten_app_id = os.environ.get("RAKUTEN_APP_ID", "")
    rakuten_access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "")
    rakuten_aff_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
    yahoo_client_id = os.environ.get("YAHOO_CLIENT_ID", "")
    vc_sid = os.environ.get("VALUECOMMERCE_SID", "")
    vc_pid = os.environ.get("VALUECOMMERCE_PID", "")

    now_iso = datetime.now(timezone.utc).isoformat()
    rakuten_index = dict(r_existing)
    yahoo_index = dict(y_existing)

    r_new, r_kept, r_skipped = 0, 0, 0
    y_new, y_kept, y_skipped = 0, 0, 0
    r_jan_hit, r_jan_miss, r_text_hit = 0, 0, 0
    y_jan_hit, y_jan_miss, y_text_hit = 0, 0, 0
    # --re-search-low-confidence 専用カウンタ
    r_low_conf_targets, y_low_conf_targets = 0, 0
    r_no_better, y_no_better = 0, 0

    for asin, (title, force_refresh, jan_code) in targets.items():
        # 既に matched があり、かつ amazon.json 由来でない (force_refresh=False) ASIN
        # → 既存エントリ保持で API コール節約
        r_has = asin in rakuten_index
        y_has = asin in yahoo_index
        need_rakuten = force_refresh or not r_has
        need_yahoo = force_refresh or not y_has

        # --re-search-low-confidence: JAN がある低信頼マッチを強制再検索対象に追加
        r_reason = ""
        y_reason = ""
        if re_search_mode and jan_code:
            amazon_price_local = amazon_price_by_asin.get(asin, 0)
            if amazon_price_local <= 0:
                amazon_price_local = _load_amazon_price_from_per_asin(per_asin_root, asin)

            r_low, r_reason = _is_low_confidence_match(
                rakuten_index.get(asin), jan_code, amazon_price_local, quality_check_fn
            )
            if r_low and not need_rakuten:
                need_rakuten = True
                r_low_conf_targets += 1

            y_low, y_reason = _is_low_confidence_match(
                yahoo_index.get(asin), jan_code, amazon_price_local, quality_check_fn
            )
            if y_low and not need_yahoo:
                need_yahoo = True
                y_low_conf_targets += 1

        if not need_rakuten and not need_yahoo:
            r_kept += 1 if r_has else 0
            y_kept += 1 if y_has else 0
            continue

        keyword = extract_search_keyword(title)
        if re_search_mode and (r_reason or y_reason):
            logger.info(
                f"Cross-searching: {keyword} (ASIN: {asin}, force={force_refresh}, "
                f"r_reason='{r_reason}', y_reason='{y_reason}')"
            )
        else:
            logger.info(f"Cross-searching: {keyword} (ASIN: {asin}, force={force_refresh})")

        # 楽天検索
        if rakuten_app_id and need_rakuten:
            r_result = search_rakuten_tiered(keyword, rakuten_app_id, rakuten_access_key, rakuten_aff_id, jan_code=jan_code, amazon_title=title, asin=asin)
            if r_result:
                r_result["matched_asin"] = asin
                r_result["search_keyword"] = keyword
                r_result["jan_code"] = jan_code
                r_result["_refreshed_at"] = now_iso
                method = r_result.get("_match_method", "text")

                # --re-search-low-confidence: no-worse-than-old guard
                # 旧エントリが quality pass で新結果が fail なら旧を保持
                replace = True
                if re_search_mode and r_has:
                    amazon_price_local = amazon_price_by_asin.get(asin, 0) or \
                        _load_amazon_price_from_per_asin(per_asin_root, asin)
                    new_passed = quality_check_fn(r_result, amazon_price_local)
                    old_passed = quality_check_fn(rakuten_index[asin], amazon_price_local)
                    if old_passed and not new_passed:
                        replace = False
                        r_no_better += 1
                        r_kept += 1
                        logger.info(
                            f"  → Rakuten: rejected new result (price={r_result.get('price')}) — "
                            f"old entry passed quality, keeping it"
                        )

                if replace:
                    if method in ("jan_books", "jan_ichiba"):
                        r_jan_hit += 1
                    else:
                        r_text_hit += 1
                        if jan_code:
                            r_jan_miss += 1
                    rakuten_index[asin] = r_result
                    r_new += 1
                    logger.info(f"  → Rakuten[{method}]: {r_result['title'][:40]}... ￥{r_result['price']}")
            else:
                # 検索失敗時、既存エントリがあれば保持 (上書きで消さない)
                if jan_code:
                    r_jan_miss += 1
                if r_has:
                    r_kept += 1
                    logger.info(f"  → Rakuten: not found (preserving existing entry)")
                else:
                    r_skipped += 1
                    logger.info(f"  → Rakuten: not found")
            time.sleep(0.5)  # Rate limit
        elif r_has:
            r_kept += 1

        # Yahoo検索
        if yahoo_client_id and need_yahoo:
            y_result = search_yahoo(keyword, yahoo_client_id, vc_sid, vc_pid, jan_code=jan_code, amazon_title=title, asin=asin)
            if y_result:
                y_result["matched_asin"] = asin
                y_result["search_keyword"] = keyword
                y_result["jan_code"] = jan_code
                y_result["_refreshed_at"] = now_iso
                method = y_result.get("_match_method", "text")

                replace = True
                if re_search_mode and y_has:
                    amazon_price_local = amazon_price_by_asin.get(asin, 0) or \
                        _load_amazon_price_from_per_asin(per_asin_root, asin)
                    new_passed = quality_check_fn(y_result, amazon_price_local)
                    old_passed = quality_check_fn(yahoo_index[asin], amazon_price_local)
                    if old_passed and not new_passed:
                        replace = False
                        y_no_better += 1
                        y_kept += 1
                        logger.info(
                            f"  → Yahoo: rejected new result (price={y_result.get('price')}) — "
                            f"old entry passed quality, keeping it"
                        )

                if replace:
                    if method == "jan":
                        y_jan_hit += 1
                    else:
                        y_text_hit += 1
                        if jan_code:
                            y_jan_miss += 1
                    yahoo_index[asin] = y_result
                    y_new += 1
                    logger.info(f"  → Yahoo[{method}]: {y_result['title'][:40]}... ￥{y_result['price']}")
            else:
                if jan_code:
                    y_jan_miss += 1
                if y_has:
                    y_kept += 1
                    logger.info(f"  → Yahoo: not found (preserving existing entry)")
                else:
                    y_skipped += 1
                    logger.info(f"  → Yahoo: not found")
            time.sleep(1.0)  # Yahoo rate limit: 1 query/sec
        elif y_has:
            y_kept += 1

    # 保存 (累積マージ後)
    out_dir.mkdir(parents=True, exist_ok=True)

    rakuten_results = list(rakuten_index.values())
    yahoo_results = list(yahoo_index.values())

    with open(out_dir / "rakuten_matched.json", "w", encoding="utf-8") as f:
        json.dump({"items": rakuten_results}, f, ensure_ascii=False, indent=4)
    logger.info(
        f"Rakuten saved: total={len(rakuten_results)} (refreshed={r_new}, kept={r_kept}, no_match={r_skipped})"
    )
    logger.info(
        f"Rakuten match-method: jan_hit={r_jan_hit}, jan_miss={r_jan_miss}, text_fallback={r_text_hit}"
    )

    with open(out_dir / "yahoo_matched.json", "w", encoding="utf-8") as f:
        json.dump({"items": yahoo_results}, f, ensure_ascii=False, indent=4)
    logger.info(
        f"Yahoo saved: total={len(yahoo_results)} (refreshed={y_new}, kept={y_kept}, no_match={y_skipped})"
    )
    logger.info(
        f"Yahoo match-method: jan_hit={y_jan_hit}, jan_miss={y_jan_miss}, text_fallback={y_text_hit}"
    )

    if re_search_mode:
        logger.info(
            f"Low-confidence re-search summary: "
            f"rakuten_targets={r_low_conf_targets} (no_better={r_no_better}), "
            f"yahoo_targets={y_low_conf_targets} (no_better={y_no_better})"
        )

    # Issue #1087 Phase 1: JAN 成否の per-ASIN manifest を出力
    _write_cross_search_manifest(out_dir, rakuten_index, yahoo_index, targets, now_iso)


def _write_cross_search_manifest(out_dir, rakuten_index, yahoo_index, targets, generated_at):
    """ASIN ごとの JAN 成否 (_jan_attempt) を集計して manifest JSON を書き出す。

    Issue #1087 Phase 1 observability: text fallback に落ちた残 6.5%/12.8% の
    edge case 群が「なぜ JAN 直引きで落ちたか」を後追いできるようにする。
    """
    rakuten_summary: dict[str, int] = {}
    yahoo_summary: dict[str, int] = {}
    items: list[dict] = []

    # targets に含まれる ASIN のみ (累積 index には古い ASIN も残るため)
    for asin, (_title, _force, jan_code) in targets.items():
        r_entry = rakuten_index.get(asin) or {}
        y_entry = yahoo_index.get(asin) or {}
        r_attempt = r_entry.get("_jan_attempt", "unknown")
        y_attempt = y_entry.get("_jan_attempt", "unknown")
        rakuten_summary[r_attempt] = rakuten_summary.get(r_attempt, 0) + 1
        yahoo_summary[y_attempt] = yahoo_summary.get(y_attempt, 0) + 1
        items.append({
            "asin": asin,
            "jan_code": jan_code or "",
            "rakuten": {
                "method": r_entry.get("_match_method", "none"),
                "jan_attempt": r_attempt,
            },
            "yahoo": {
                "method": y_entry.get("_match_method", "none"),
                "jan_attempt": y_attempt,
            },
        })

    manifest = {
        "generated_at": generated_at,
        "summary": {
            "rakuten": dict(sorted(rakuten_summary.items())),
            "yahoo": dict(sorted(yahoo_summary.items())),
            # #2186 P1: 上流マッチャが弾いた中古/型番不一致の理由別件数。
            "prevent_drops": dict(sorted(_DROP_STATS.items())),
        },
        "items": items,
    }
    manifest_path = out_dir / "_cross_search_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        f"Cross-search manifest saved: {manifest_path} "
        f"(rakuten={rakuten_summary}, yahoo={yahoo_summary})"
    )
    if _DROP_STATS:
        logger.info(f"Prevent-side drops (used/model mismatch): {dict(sorted(_DROP_STATS.items()))}")


if __name__ == "__main__":
    main()
