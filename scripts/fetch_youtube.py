"""
fetch_youtube.py
ジャンル全体 (--keyword) 検索に加え、data/raw/amazon.json の各 ASIN について
brand+model 番号での個別検索も実行し、結果をマージして data/raw/youtube.json に保存する。

Phase 2 (3a) で追加した filter_raw_per_asin.py が参照するプールに、
ASIN と関連性の高い動画が含まれるようにするための改修。
"""

import os
import sys
import json
import re
import logging
import pathlib
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_youtube")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# 既知ブランド (filter_raw_per_asin.py と一致させること)
KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "ボーネルンド",
    "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー", "セガトイズ",
    "エポック", "アガツマ", "ジョイレア",
]


def get_secret(name: str) -> str:
    return os.environ.get(name)


# 複数 API key を順次フォールバックする state。
# 各 key は別 Google Cloud project の YOUTUBE Data API key を想定 (project ごとに
# daily quota 10,000 units を持つ)。secret 名は YOUTUBE_API_KEY,
# YOUTUBE_API_KEY2, YOUTUBE_API_KEY3, YOUTUBE_API_KEY4。未登録 (空文字) の slot は
# load 時に除外する。403 + body に "quota" or "exceeded" を含むレスポンスを
# 受け取ったら、その key を exhausted として捨てて次の key に切替える。全 key
# 枯渇した時点で youtube_search は [] を返し、以降のクエリも空で抜ける。
_API_KEYS: list[str] = []
_KEY_INDEX: int = 0
_EXHAUSTED_LOGGED: set[int] = set()


def load_api_keys() -> list[str]:
    """secret 名 YOUTUBE_API_KEY / *_KEY2 / *_KEY3 / *_KEY4 をこの順に読み、
    非空のものだけ list で返す。"""
    keys: list[str] = []
    for name in ("YOUTUBE_API_KEY", "YOUTUBE_API_KEY2",
                 "YOUTUBE_API_KEY3", "YOUTUBE_API_KEY4"):
        v = get_secret(name)
        if v:
            keys.append(v)
    return keys


def _is_quota_error(status: int, body: str) -> bool:
    if status != 403:
        return False
    low = (body or "").lower()
    return "quota" in low or "exceeded" in low


def _current_key() -> str | None:
    if _KEY_INDEX >= len(_API_KEYS):
        return None
    return _API_KEYS[_KEY_INDEX]


def _rotate_key(reason_body: str) -> bool:
    """現在の key を枯渇扱いにして次の key に切替える。次が無ければ False。"""
    global _KEY_INDEX
    prev = _KEY_INDEX
    if prev not in _EXHAUSTED_LOGGED:
        logger.warning(
            f"YOUTUBE_API_KEY slot #{prev + 1} quota exhausted "
            f"(body excerpt: {reason_body[:200]})"
        )
        _EXHAUSTED_LOGGED.add(prev)
    _KEY_INDEX += 1
    nxt = _current_key()
    if nxt is None:
        logger.warning("All YouTube API keys exhausted; remaining queries will return empty.")
        return False
    logger.info(f"Switching to YOUTUBE_API_KEY slot #{_KEY_INDEX + 1}")
    return True


def extract_model_number(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4})\b", text)
    if m:
        return m.group(1)
    return ""


def extract_brand(text: str) -> str:
    for b in KNOWN_BRANDS:
        if b in text:
            return b
    return ""


def build_per_asin_query(title: str) -> str:
    """ASIN タイトルから YouTube 検索向けクエリ文字列を作る。
    優先度: brand+モデル番号 > brand+先頭2-3トークン > タイトル先頭30字"""
    brand = extract_brand(title)
    model = extract_model_number(title)
    if brand and model:
        return f"{brand} {model}"
    if brand:
        # ブランド + シリーズらしい部分 (タイトルの先頭から2語)
        clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title).replace(brand, "")
        tokens = [t for t in clean.split() if len(t) >= 2][:2]
        return f"{brand} {' '.join(tokens)}".strip()
    return title[:30]


def _do_search_once(api_key: str, query: str, max_results: int) -> tuple[int, str, list]:
    """1 回の search.list 呼び出し。(status_code, body_text, items) を返す。
    items は 200 のときのみ非空。例外時は (0, str(e), [])。"""
    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "maxResults": max_results,
        "type": "video",
        "relevanceLanguage": "ja",
        "regionCode": "JP",
    }
    try:
        resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
        if resp.status_code != 200:
            return resp.status_code, resp.text, []
        data = resp.json()
        out = []
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            sn = item.get("snippet", {})
            out.append({
                "title": sn.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": sn.get("thumbnails", {}).get("high", {}).get("url", ""),
            })
        return 200, "", out
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", []


def youtube_search(query: str, max_results: int = 5) -> list:
    """quota 枯渇時は次の API key にローテートして同じ query を 1 回ずつ再試行する。
    全 key 枯渇後の query は即 [] を返す。
    quota 以外の 4xx/5xx はその query 限りの失敗 (key はローテートしない)。"""
    while True:
        key = _current_key()
        if key is None:
            return []
        status, body, items = _do_search_once(key, query, max_results)
        if status == 200:
            return items
        if _is_quota_error(status, body):
            if _rotate_key(body):
                # 新しい key で同じ query をもう一度
                continue
            # 全 key 枯渇
            return []
        # quota 以外のエラー (keyInvalid / 5xx / network) はこの query だけ諦める
        logger.warning(
            f"YouTube search failed for '{query}': HTTP {status} body={body[:300]}"
        )
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="知育玩具")
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    global _API_KEYS, _KEY_INDEX, _EXHAUSTED_LOGGED
    _API_KEYS = load_api_keys()
    _KEY_INDEX = 0
    _EXHAUSTED_LOGGED = set()
    items = []

    if not _API_KEYS:
        logger.warning("YouTube API key missing (no YOUTUBE_API_KEY{,2,3,4}). Skipping fetch.")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False, indent=4)
        return

    logger.info(f"Loaded {len(_API_KEYS)} YouTube API key(s) for quota fallback.")
    seen_urls = set()

    # 1. ジャンル全体検索
    genre_kw = args.keyword if args.keyword else "知育玩具"
    logger.info(f"Genre search: {genre_kw}")
    for v in youtube_search(f"{genre_kw} おもちゃ レビュー", max_results=5):
        if v["url"] not in seen_urls:
            items.append(v)
            seen_urls.add(v["url"])

    # 2. ASIN 別検索 (per_asin/ フィルタが意味あるコンテンツを得られるように)
    amazon_path = pathlib.Path(args.out) / "amazon.json"
    if amazon_path.exists():
        try:
            amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
            asin_items = amazon.get("items", [])
            logger.info(f"Per-ASIN search: {len(asin_items)} ASINs")
            for amz in asin_items:
                title = amz.get("title", "")
                if not title:
                    continue
                query = build_per_asin_query(title)
                if not query:
                    continue
                logger.info(f"  [{amz.get('asin')}] query='{query}'")
                for v in youtube_search(query, max_results=3):
                    if v["url"] not in seen_urls:
                        items.append(v)
                        seen_urls.add(v["url"])
        except Exception as e:
            logger.warning(f"Per-ASIN YouTube search skipped: {e}")
    else:
        logger.info("amazon.json not found, skipping per-ASIN search")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "youtube.json"), "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(items)} videos to youtube.json")


if __name__ == "__main__":
    main()
