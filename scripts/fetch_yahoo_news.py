"""
fetch_yahoo_news.py
ジャンル横断 + amazon.json の各 ASIN について Google News RSS で関連ニュースを取得し、
data/raw/news.json に items[] フラット形式で保存する。
filter_raw_per_asin.py が後段で ASIN 別にスコアリングして per_asin/<ASIN>/news.json に振り分ける。

旧実装は news.yahoo.co.jp の life カテゴリ RSS のみ取得していたが、グルメ/宿泊系の
知育玩具と無関係な内容で関連度フィルタを通らず per_asin/news.json が全 ASIN 空だった。
"""

import argparse
import json
import logging
import os
import pathlib
import re
import xml.etree.ElementTree as ET

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_yahoo_news")

GNEWS_URL = "https://news.google.com/rss/search"

NOISE = {
    "送料無料", "ポイント10倍", "正規品", "公式", "最新", "予約",
    "おまけ付き", "ラッピング無料", "あす楽", "即納", "税込",
    "知育玩具", "おもちゃ", "プレゼント", "誕生日", "ギフト",
    "2個セット", "3歳から", "男の子", "女の子", "対象年齢",
    "周年", "記念", "限定", "シリーズ", "セット", "コレクション",
    "スペシャル", "オリジナル", "デラックス", "プレミアム", "新品",
    "保護", "対象", "本体", "付属", "別売", "マーク", "認証", "推奨",
    "規格", "対応", "簡単", "便利", "玩具", "子供", "知育",
}

MODEL_PATTERN = re.compile(r"^([A-Z][A-Z0-9\-]{3,11}|\d{4,5})$")

# ニュース検索では「ブランド名 + 商品語」が当たりやすい。短いブランド
# (レゴ/トミカ) は _QUERY_JA (3 字以上) で漏れるため明示的に prepend する。
KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "ボーネルンド",
    "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー", "セガトイズ",
    "セガフェイブ", "エポック", "アガツマ", "ジョイレア", "Hape", "ホイップる",
]


def _extract_brand(text: str) -> str:
    for b in KNOWN_BRANDS:
        if b in text:
            return b
    return ""
_QUERY_SPLIT = re.compile(r"[\s！。、・/／,!\?？:：「」『』\-]+")
_QUERY_TRAIL = re.compile(
    r"(\d{1,4}周年(記念)?|記念|限定|新品|BOX|セット|版|号|\d{4}年|"
    r"スペシャル|オリジナル|コレクション)$"
)
_QUERY_JA = re.compile(r"[ぁ-んァ-ヶー一-龯]{3,12}")
_QUERY_VERB = re.compile(r"^[ぁ-ん]{3,5}[うるく]$")
_QUERY_LEAD_PARTICLE = re.compile(r"^[系型形]+")


def _extract_terms(title: str) -> list[str]:
    """タイトルから商品同定に効く 3-12 字語を出現順に抽出。
    fetch_youtube._extract_query_terms と対称な実装 (brand 除去はここでは
    しない: 検索クエリには brand も入れたいため、brand 含む chunk もそのまま
    返し、呼び出し側で brand を頭に置く)。"""
    if not title:
        return []
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    clean = re.sub(r"\(C\).*?(?=\s|$)", "", clean)
    seen: set[str] = set()
    out: list[str] = []
    for chunk in _QUERY_SPLIT.split(clean):
        chunk = chunk.strip()
        if not chunk:
            continue
        for m in _QUERY_JA.finditer(chunk):
            t = m.group(0)
            for _ in range(2):
                s = _QUERY_TRAIL.sub("", t)
                if s == t:
                    break
                t = s
            t = _QUERY_LEAD_PARTICLE.sub("", t)
            if len(t) < 3 or t in NOISE or _QUERY_VERB.match(t):
                continue
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def extract_keyword(title: str) -> str:
    """Amazon タイトルから Google News 検索クエリを抽出。

    旧実装は `clean.split()` で先頭 3 語を採用していたが、空白の少ない
    日本語タイトル (例: "アンパンマン にほんごえいご二語文も！あそぼう！…
    ことばずかん15周年記念BOX") では区切れず 40 字切り捨ての無意味クエリに
    なっていた。新実装は句読点でも分割し、3-12 字の同定語を最大 3 つ
    (長さ降順 + tie は後方優先) 拾って結合する。
    """
    if not title:
        return ""
    brand = _extract_brand(title)
    terms = [t for t in _extract_terms(title) if t != brand]
    if not terms and not brand:
        return title[:40]
    # 商品名末尾優先 + 識別性 (長さ) を兼ねた採用順
    indexed = [(len(t), idx, t) for idx, t in enumerate(terms)]
    indexed.sort(key=lambda x: (-x[0], -x[1]))
    picked = [t for _, _, t in indexed[:3]]
    parts = ([brand] if brand else []) + picked
    return " ".join(parts)[:60].strip()


def gnews_search(query: str, max_results: int = 5) -> list:
    params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    try:
        resp = requests.get(GNEWS_URL, params=params, timeout=15)
        if resp.status_code >= 400:
            logger.warning(f"Google News {resp.status_code} for '{query}'")
            return []
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.warning(f"Google News error for '{query}': {e}")
        return []
    out = []
    for item in root.findall(".//item")[:max_results]:
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        if title_el is None or link_el is None:
            continue
        out.append({
            "title": title_el.text or "",
            "url": link_el.text or "",
            "published": pub_el.text if pub_el is not None else "",
        })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--keyword", default="知育玩具",
                        help="ジャンル全体検索クエリ (フォールバック)")
    args = parser.parse_args()

    items = []
    seen_urls = set()

    logger.info(f"Genre search: {args.keyword}")
    for n in gnews_search(args.keyword, max_results=5):
        url = n.get("url") or ""
        if url and url not in seen_urls:
            items.append(n)
            seen_urls.add(url)

    base_dir = pathlib.Path(__file__).resolve().parent.parent
    amazon_path = base_dir / "data" / "raw" / "amazon.json"
    if amazon_path.exists():
        try:
            amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
            asin_items = amazon.get("items", [])
            logger.info(f"Per-ASIN search: {len(asin_items)} ASINs")
            seen_queries = set()
            for amz in asin_items:
                query = extract_keyword(amz.get("title", ""))
                if not query or query in seen_queries:
                    continue
                seen_queries.add(query)
                logger.info(f"  query='{query}'")
                for n in gnews_search(query, max_results=3):
                    url = n.get("url") or ""
                    if url and url not in seen_urls:
                        items.append(n)
                        seen_urls.add(url)
        except Exception as e:
            logger.warning(f"Per-ASIN news search skipped: {e}")
    else:
        logger.info("amazon.json not found, skipping per-ASIN search")

    os.makedirs(args.out, exist_ok=True)
    save_path = os.path.join(args.out, "news.json")
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved {len(items)} news items to {save_path}")


if __name__ == "__main__":
    main()
