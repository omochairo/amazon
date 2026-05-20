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

import _fetch_targets

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


# 商品同定に効かないサフィックス/汎用語。product_term 候補から外す。
_QUERY_NOISE = {
    "知育玩具", "おもちゃ", "玩具", "プレゼント", "誕生日", "ギフト", "男の子",
    "女の子", "子供", "知育", "周年", "記念", "限定", "シリーズ", "セット",
    "コレクション", "スペシャル", "オリジナル", "デラックス", "プレミアム",
    "公式", "新品", "保護", "対象", "本体", "付属", "別売", "マーク", "認証",
    "推奨", "規格", "対応", "簡単", "便利",
}
_QUERY_LEAD_PARTICLE = re.compile(r"^[系型形]+")
_QUERY_SPLIT = re.compile(r"[\s！。、・/／,!\?？:：「」『』\-]+")
_QUERY_TRAIL = re.compile(
    r"(\d{1,4}周年(記念)?|記念|限定|新品|BOX|セット|版|号|\d{4}年|"
    r"スペシャル|オリジナル|コレクション)$"
)
_QUERY_JA = re.compile(r"[ぁ-んァ-ヶー一-龯]{3,12}")
_QUERY_VERB = re.compile(r"^[ぁ-ん]{3,5}[うるく]$")


def _extract_query_terms(title: str, brand: str) -> list[str]:
    """タイトルから商品同定に効く 3-12 字の語を抽出 (重複排除しつつ出現順を保持)。
    ブランド除去・括弧除去・サフィックス剥がし・動詞風 (あそぼう/しゃべろう) 除外。"""
    if not title:
        return []
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    if brand:
        clean = clean.replace(brand, " ")
    seen: set[str] = set()
    out: list[str] = []
    for chunk in _QUERY_SPLIT.split(clean):
        chunk = chunk.strip()
        if not chunk:
            continue
        for m in _QUERY_JA.finditer(chunk):
            t = m.group(0)
            for _ in range(2):
                stripped = _QUERY_TRAIL.sub("", t)
                if stripped == t:
                    break
                t = stripped
            # 先頭の "系/型/形" 助辞 (例: "系総武線" → "総武線") を剥がす
            t = _QUERY_LEAD_PARTICLE.sub("", t)
            if len(t) < 3 or t in _QUERY_NOISE or _QUERY_VERB.match(t):
                continue
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def build_per_asin_query(title: str) -> str:
    """ASIN タイトルから YouTube 検索クエリを作る。
    優先度:
      1. brand + 4-5 桁モデル番号 (LEGO 71439 等の最も識別性が高い)
      2. brand + 商品語 2 つ (例: アンパンマン ことばずかん にほんごえいご二語文)
      3. brand + 商品語 1 つ
      4. brand 単独
      5. タイトル先頭 30 字 (ブランド未検出時のフォールバック)
    商品語は _extract_query_terms で抽出 (句読点・周年記念BOX 等のサフィックス
    剥がし + 動詞風語の除外を含む)。先頭から最長 2 つを採用。
    """
    brand = extract_brand(title)
    model = extract_model_number(title)
    if brand and model and len(model) >= 4:
        return f"{brand} {model}"
    terms = _extract_query_terms(title, brand)
    # 長い順 (識別性高い) + 同点はタイトル後方優先 (商品名は末尾寄りに来ることが
    # 多いので、tie 時に後方の語を優先する)。最大 3 つまで採用。
    indexed = [(len(t), idx, t) for idx, t in enumerate(terms)]
    indexed.sort(key=lambda x: (-x[0], -x[1]))
    picked = [t for _, _, t in indexed[:3]]
    if brand and picked:
        return f"{brand} {' '.join(picked)}"
    if brand and model:
        return f"{brand} {model}"
    if brand:
        return brand
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
    parser.add_argument("--articles-dir", default="data/articles",
                        help="article ASIN を per-ASIN target に union するソース")
    parser.add_argument("--max-per-run", type=int, default=50,
                        help="1 run あたりの per-ASIN query 上限 (quota 制御)")
    parser.add_argument("--stale-after-days", type=int, default=7,
                        help="この日数以内に query 済の ASIN はスキップ")
    args = parser.parse_args()

    global _API_KEYS, _KEY_INDEX, _EXHAUSTED_LOGGED
    _API_KEYS = load_api_keys()
    _KEY_INDEX = 0
    _EXHAUSTED_LOGGED = set()
    items = []

    out_dir = pathlib.Path(args.out)

    if not _API_KEYS:
        logger.warning("YouTube API key missing (no YOUTUBE_API_KEY{,2,3,4}). Skipping fetch.")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "youtube.json").write_text(
            json.dumps({"items": []}, ensure_ascii=False, indent=4), encoding="utf-8")
        return

    logger.info(f"Loaded {len(_API_KEYS)} YouTube API key(s) for quota fallback.")
    seen_urls = set()

    # 1. ジャンル全体検索 (global pool — 全 ASIN の filter で共通使用)
    genre_kw = args.keyword if args.keyword else "知育玩具"
    logger.info(f"Genre search: {genre_kw}")
    for v in youtube_search(f"{genre_kw} おもちゃ レビュー", max_results=5):
        if v["url"] not in seen_urls:
            items.append(v)
            seen_urls.add(v["url"])

    # 2. ASIN 別検索 (stale-first 巡回、quota 制御)
    #    target = union(amazon.json, data/articles/*.json) を _fetch_targets で
    #    staleness 順に並べて先頭 max_per_run のみ query。Quota が枯渇したら
    #    youtube_search が [] を返すので残りは自動的に no-op。
    amazon_items = []
    amazon_path = out_dir / "amazon.json"
    if amazon_path.exists():
        try:
            amazon_items = json.loads(amazon_path.read_text(encoding="utf-8")).get("items", [])
        except json.JSONDecodeError:
            logger.warning("amazon.json unreadable; using article targets only")

    targets = _fetch_targets.pick_target_asins(
        out_dir=out_dir,
        source="youtube",
        amazon_items=amazon_items,
        articles_dir=pathlib.Path(args.articles_dir),
        max_per_run=args.max_per_run,
        stale_after_days=args.stale_after_days,
    )

    queried_asins: list[str] = []
    for asin, title in targets:
        if not title:
            continue
        query = build_per_asin_query(title)
        if not query:
            continue
        logger.info(f"  [{asin}] query='{query}'")
        per_asin_items: list = []
        for v in youtube_search(query, max_results=3):
            per_asin_items.append(v)
            if v["url"] not in seen_urls:
                items.append(v)
                seen_urls.add(v["url"])
        # Empty result も含めて raw を書く (= 次 run までスキップ対象になる)
        _fetch_targets.write_per_asin_raw(out_dir, "youtube", asin, query, per_asin_items)
        queried_asins.append(asin)

    # state を一括更新 (空 result でも mark することで retry loop 防止)
    if queried_asins:
        _fetch_targets.mark_queried(out_dir, "youtube", queried_asins)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "youtube.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=4), encoding="utf-8")
    logger.info(f"Saved {len(items)} videos to youtube.json (queried {len(queried_asins)} ASINs)")


if __name__ == "__main__":
    main()
