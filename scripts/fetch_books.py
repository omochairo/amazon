"""
fetch_books.py
ジャンル全体 (--keyword) 検索に加え、data/raw/amazon.json の各 ASIN について
ブランド名での個別検索も実行し、結果をマージして data/raw/books_result.json に保存する。

書籍は商品モデル番号で hit しないため、ブランド固有のジャンル語彙
(キャラ系=絵本 / 鉄道系=のりもの / STEM系=図鑑 など) を組み合わせて
検索する。Issue #503 / #560 (2026-05-31): "ブランド 知育" 単独だと Google
Books に殆ど hit せず books 充足率が 0% で停滞していたため、ジャンル別
プライマリ + フォールバックチェーン + 旅行ガイド NG フィルタを導入。
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
logger = logging.getLogger("fetch_books")

BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"

KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "ボーネルンド",
    "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー", "セガトイズ",
    "エポック", "アガツマ", "ジョイレア",
]

NOISE = [
    "送料無料", "ポイント10倍", "正規品", "公式", "最新", "予約",
    "おまけ付き", "ラッピング無料", "あす楽", "即納", "税込",
    "知育玩具", "おもちゃ", "プレゼント", "誕生日", "ギフト",
    "2個セット", "3歳から", "男の子", "女の子", "対象年齢",
]

# #503: brand 別の最適 secondary keyword。デフォルトは "絵本" (Google Books に
# 最も hit しやすい子供向け書籍カテゴリ)。鉄道・乗り物系は "のりもの" のほうが
# シリーズ図鑑にヒットしやすい。STEM/知育玩具系は "図鑑" を優先する。
# キーは KNOWN_BRANDS の表記に揃える。未収録ブランドは default ("絵本") を使う。
BRAND_GENRE_VOCAB = {
    # キャラ・絵本系
    "アンパンマン": "絵本",
    "ディズニー": "絵本",
    "サンリオ": "絵本",
    "ポケモン": "絵本",
    "すみっコぐらし": "絵本",
    "リカちゃん": "絵本",
    "シルバニアファミリー": "絵本",
    # 鉄道・乗り物系
    "プラレール": "のりもの",
    "トミカ": "のりもの",
    # STEM・知育玩具系
    "レゴ": "図鑑",
    "LEGO": "図鑑",
    "くもん": "ドリル",
    "公文": "ドリル",
    "学研": "図鑑",
    "ピープル": "図鑑",
    "ボーネルンド": "図鑑",
    # 大手メーカー (キャラ依存度高いので絵本)
    "バンダイ": "絵本",
    "タカラトミー": "絵本",
    "セガトイズ": "絵本",
    "エポック": "絵本",
    "アガツマ": "絵本",
    "ジョイレア": "絵本",
}

# #560: マッチ精度を下げる「子供 + 広域語」で偶発 hit しやすい書籍ファミリーを
# タイトル単位で弾く。代表例:
#   - るるぶこどもとあそぼ / まっぷるキッズ (旅行ガイド)
#   - 〇〇県年鑑 / 年版 (年版もの)
#   - 観光案内 / 旅行ガイド / 都道府県ガイド
#   - 中古試験問題 / 公務員試験 (大人向け資格本が「子供」と無関係でヒット)
# `re.search` で大文字小文字無視 + 全角半角差は別パターンで吸収する。
BOOKS_NG_PATTERNS = [
    r"るるぶ",
    r"まっぷる",
    r"観光ガイド",
    r"旅行ガイド",
    r"地球の歩き方",
    # 年版書籍 ('15～'16 / 2024年版 / 2024-2025 など)
    r"['']\d{2}\s*[~〜～-]\s*['']?\d{2}",
    r"20\d{2}\s*[~〜～-]\s*20\d{2}",
    r"20\d{2}\s*年版",
    # 大人向け資格・試験本
    r"公務員試験",
    r"宅建士",
    r"FP\s*\d級",
    # 婦人雑誌・実用書
    r"婦人画報",
    r"きょうの料理",
]
_NG_RE = re.compile("|".join(BOOKS_NG_PATTERNS), re.IGNORECASE)


def is_books_noise(title: str | None) -> bool:
    """``BOOKS_NG_PATTERNS`` のいずれかにマッチする書籍タイトルなら True。

    #560: 「ジグソーパズル → るるぶこどもとあそぼ」のように、子供 + 広域語で
    偶発的に strict filter を通過してしまう旅行ガイド/年鑑ファミリーを弾く。
    """
    if not title:
        return False
    return bool(_NG_RE.search(title))


def _genre_query_for_brand(brand: str) -> str:
    """ブランド別 secondary keyword を返す。未収録なら ``"絵本"``。"""
    return BRAND_GENRE_VOCAB.get(brand, "絵本")


def build_query_chain(brand: str, fallback_kw: str) -> list[str]:
    """#503: per-ASIN query の段階フォールバック列を返す。

    primary: ``<brand> <genre>`` (brand あり) / ``<fallback_kw> 絵本`` (brand なし)
    second:  ``<brand>`` 単独 (brand あり)
    どちらの段階で hit したかは呼び出し側が API レスポンスを見て決める。
    最大 2 段までに抑えて API quota を節約する。
    """
    chain: list[str] = []
    if brand:
        chain.append(f"{brand} {_genre_query_for_brand(brand)}")
        chain.append(brand)
    elif fallback_kw:
        chain.append(f"{fallback_kw} 絵本")
    # 重複と空文字を除去
    seen: set[str] = set()
    out: list[str] = []
    for q in chain:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def extract_brand(text: str) -> str:
    for b in KNOWN_BRANDS:
        if b in text:
            return b
    return ""


def extract_fallback_keyword(title: str) -> str:
    """KNOWN_BRANDS にヒットしないタイトルから検索語を抽出。
    括弧/ノイズ語除去後の先頭 2 語を使う。"""
    if not title:
        return ""
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    for n in NOISE:
        clean = clean.replace(n, " ")
    tokens = clean.split()
    if not tokens:
        return ""
    return " ".join(tokens[:2]).strip()


def books_search(api_key: str, query: str, max_results: int = 3) -> list:
    params = {
        "q": query,
        "key": api_key,
        "maxResults": max_results,
        "langRestrict": "ja",
        "orderBy": "relevance",
    }
    try:
        resp = requests.get(BOOKS_URL, params=params, timeout=15)
        if resp.status_code >= 500:
            logger.warning(f"Google Books 5xx for '{query}'")
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Books search error for '{query}': {e}")
        return []

    out = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        title = info.get("title")
        # #560: 旅行ガイド/年鑑ファミリーをここで捨てる (per_asin/books.json まで
        # 残らないように)。description も併せて見るのは過剰なので title のみ。
        if is_books_noise(title):
            logger.info(f"  drop NG book: {title!r}")
            continue
        thumbnail = info.get("imageLinks", {}).get("thumbnail") \
            or info.get("imageLinks", {}).get("smallThumbnail")
        if thumbnail and thumbnail.startswith("http://"):
            thumbnail = thumbnail.replace("http://", "https://")
        out.append({
            "title": title,
            "authors": info.get("authors", ["不明"]),
            "description": info.get("description", "説明なし"),
            "url": info.get("infoLink"),
            "image": thumbnail,
        })
    return out


def main():
    import argparse
    parser = argparse.ArgumentParser()
    # sys.argv[1] 互換のための positional (旧呼出 `fetch_books.py "<keyword>"`)
    parser.add_argument("keyword", nargs="?", default="知育")
    parser.add_argument("--out", default=None,
                        help="出力先 (省略時は repo root/data/raw/)")
    parser.add_argument("--articles-dir", default=None,
                        help="article ASIN を per-ASIN target に union するソース")
    parser.add_argument("--max-per-run", type=int, default=50,
                        help="1 run あたりの per-ASIN query 上限")
    parser.add_argument("--stale-after-days", type=int, default=7,
                        help="この日数以内に query 済の ASIN はスキップ")
    args = parser.parse_args()
    keyword = args.keyword or "知育"

    api_key = os.environ.get("GOOGLEBOOKS_API_KEY")
    base_dir = pathlib.Path(__file__).resolve().parent.parent
    out_dir = pathlib.Path(args.out) if args.out else (base_dir / "data" / "raw")
    articles_dir = pathlib.Path(args.articles_dir) if args.articles_dir else (base_dir / "data" / "articles")
    save_path = out_dir / "books_result.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not api_key:
        logger.warning("GOOGLEBOOKS_API_KEY missing. Skipping fetch.")
        save_path.write_text(json.dumps({"keyword": keyword, "items": []}, ensure_ascii=False, indent=4),
                             encoding="utf-8")
        return

    items = []
    seen_urls = set()

    # 1. ジャンル全体検索
    genre_query = f"{keyword} 絵本" if "知育" in keyword else f"{keyword} 知育 絵本"
    logger.info(f"Genre search: {genre_query}")
    for b in books_search(api_key, genre_query, max_results=3):
        url = b.get("url") or ""
        if url and url not in seen_urls:
            items.append(b)
            seen_urls.add(url)

    # 2. ASIN 別検索 (stale-first 巡回)
    amazon_items = []
    amazon_path = out_dir / "amazon.json"
    if amazon_path.exists():
        try:
            amazon_items = json.loads(amazon_path.read_text(encoding="utf-8")).get("items", [])
        except json.JSONDecodeError:
            logger.warning("amazon.json unreadable; using article targets only")

    targets = _fetch_targets.pick_target_asins(
        out_dir=out_dir,
        source="books",
        amazon_items=amazon_items,
        articles_dir=articles_dir,
        max_per_run=args.max_per_run,
        stale_after_days=args.stale_after_days,
    )

    # ブランド共通の query になりやすいので same-query dedup (API 節約)
    seen_queries: set[str] = set()
    queried_asins: list[str] = []
    for asin, title in targets:
        brand = extract_brand(title)
        fb = "" if brand else extract_fallback_keyword(title)
        if not brand and not fb:
            continue
        # #503: brand 別 vocab + fallback chain。最大 2 段で primary が hit
        # しないときだけ次段に進む (API quota 節約)。
        query_chain = build_query_chain(brand, fb)
        if not query_chain:
            continue
        per_asin_items: list = []
        used_query = ""
        for query in query_chain:
            if query in seen_queries:
                # 既に他 ASIN で叩いた query は再度叩かない (API 節約)。
                # ただし stage 進行は止めず、次の query があれば試す。
                logger.info(f"  [{asin}] query='{query}' (deduped, no API call)")
                used_query = used_query or query
                continue
            seen_queries.add(query)
            logger.info(f"  [{asin}] brand='{brand or '-'}' query='{query}'")
            fetched = books_search(api_key, query, max_results=2)
            for b in fetched:
                per_asin_items.append(b)
                url = b.get("url") or ""
                if url and url not in seen_urls:
                    items.append(b)
                    seen_urls.add(url)
            used_query = query
            if per_asin_items:
                # primary で hit したら fallback 段は試さない。
                break
        _fetch_targets.write_per_asin_raw(
            out_dir, "books", asin, used_query or query_chain[0], per_asin_items
        )
        queried_asins.append(asin)

    if queried_asins:
        _fetch_targets.mark_queried(out_dir, "books", queried_asins)

    save_path.write_text(json.dumps({"keyword": keyword, "items": items}, ensure_ascii=False, indent=4),
                         encoding="utf-8")
    logger.info(f"Saved {len(items)} books to {save_path} (queried {len(queried_asins)} ASINs)")


if __name__ == "__main__":
    main()
