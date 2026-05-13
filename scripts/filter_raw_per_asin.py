"""
filter_raw_per_asin.py
Amazon ASIN ごとに、ジャンル全体取得された
data/raw/{youtube,news,books_result}.json を関連度スコアでフィルタし、
data/raw/per_asin/<ASIN>/{youtube,news,books}.json に top-N で書き出す。

Jules はこの per_asin/<ASIN>/ のみを参照することで、
ジャンル横断の無関係な動画/ニュース/書籍が記事に混入することを防ぐ。

スコアリング戦略:
  1. brand 完全一致      +5.0
  2. model 番号一致      +10.0  (LEGO 71439 等の固有 SKU)
  3. series 名一致       +3.0   (例: スーパーマリオ / プラレール)
  4. token 重複 (bigram) +0.5 each (上限 +3.0)

スコア閾値 SCORE_THRESHOLD 以上のもののみ保存。
"""

import json
import logging
import pathlib
import re

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("filter_raw_per_asin")

SCORE_THRESHOLD = 3.0
TOP_N = 5

# 既知ブランド/シリーズ辞書 (拡張可)
KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "BorneLund",
    "ボーネルンド", "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー",
    "セガトイズ", "エポック", "アガツマ", "ジョイレア", "Joyreal",
]

KNOWN_SERIES = [
    "スーパーマリオ", "マリオカート", "クラシック", "デュプロ", "シティ", "フレンズ",
    "ニンジャゴー", "ハリー・ポッター", "アイデアパーツ", "ビジーボード", "ビッグステーション",
    "わくわく", "ジスター", "アイデアボックス",
]

NOISE = {
    "送料無料", "ポイント10倍", "正規品", "公式", "最新", "予約", "おまけ付き",
    "ラッピング無料", "あす楽", "即納", "税込", "知育玩具", "おもちゃ", "玩具",
    "プレゼント", "誕生日", "ギフト", "男の子", "女の子", "子供", "知育",
}


def extract_model_number(text: str) -> str:
    """LEGO/トミカ系の 4-5 桁モデル番号を抽出。先頭一致を優先。"""
    if not text:
        return ""
    # 5 桁を優先 (71439 等)、次に 4 桁
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4})\b", text)
    if m:
        return m.group(1)
    return ""


def extract_brand_series(text: str) -> tuple[set, set]:
    """テキストから既知のブランド・シリーズを抽出。"""
    if not text:
        return set(), set()
    brands = {b for b in KNOWN_BRANDS if b in text}
    series = {s for s in KNOWN_SERIES if s in text}
    return brands, series


def tokenize(text: str) -> set:
    """日本語テキストを bigram + ASCII 単語に分解。簡易版。"""
    if not text:
        return set()
    # 括弧除去
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", text)
    # ノイズ語除去
    for n in NOISE:
        clean = clean.replace(n, " ")
    tokens = set()
    # ASCII (LEGO 等) と数字をそのまま
    for m in re.finditer(r"[A-Za-z]{2,}|\d{3,}", clean):
        tokens.add(m.group(0).lower())
    # 日本語は bigram (2 文字単位) で類似度判定
    ja_only = re.sub(r"[^ぁ-んァ-ヶ一-龯]", " ", clean)
    for chunk in ja_only.split():
        for i in range(len(chunk) - 1):
            bg = chunk[i : i + 2]
            if bg.strip():
                tokens.add(bg)
    return tokens


def score_item(item_text: str, asin_brands: set, asin_series: set,
               asin_model: str, asin_tokens: set) -> float:
    """1 アイテムのテキストに対する関連度スコア。"""
    if not item_text:
        return 0.0
    score = 0.0
    item_brands, item_series = extract_brand_series(item_text)
    if asin_brands & item_brands:
        score += 5.0
    if asin_model and asin_model in item_text:
        score += 10.0
    if asin_series & item_series:
        score += 3.0
    item_tokens = tokenize(item_text)
    overlap = len(asin_tokens & item_tokens)
    score += min(overlap * 0.5, 3.0)
    return score


def filter_items(raw_items: list, asin_brands: set, asin_series: set,
                 asin_model: str, asin_tokens: set,
                 text_keys: list[str]) -> list:
    """raw 配列をスコアリングして閾値以上を返す。"""
    scored = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(k, "")) for k in text_keys)
        s = score_item(text, asin_brands, asin_series, asin_model, asin_tokens)
        if s >= SCORE_THRESHOLD:
            enriched = dict(item)
            enriched["_relevance_score"] = round(s, 2)
            scored.append(enriched)
    scored.sort(key=lambda x: x["_relevance_score"], reverse=True)
    return scored[:TOP_N]


def main():
    root = pathlib.Path(".")
    raw_dir = root / "data" / "raw"
    amazon_path = raw_dir / "amazon.json"
    if not amazon_path.exists():
        logger.error(f"{amazon_path} not found")
        return

    amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
    items = amazon.get("items", [])
    if not items:
        logger.warning("amazon.json has no items")
        return

    youtube_items = json.loads((raw_dir / "youtube.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "youtube.json").exists() else []
    news_items = json.loads((raw_dir / "news.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "news.json").exists() else []
    books_items = json.loads((raw_dir / "books_result.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "books_result.json").exists() else []

    logger.info(f"Source pools: youtube={len(youtube_items)} news={len(news_items)} books={len(books_items)}")

    out_root = raw_dir / "per_asin"
    out_root.mkdir(parents=True, exist_ok=True)

    summary = []
    for amz in items:
        asin = amz.get("asin", "")
        title = amz.get("title", "")
        if not asin:
            continue

        brands, series = extract_brand_series(title)
        model = extract_model_number(title)
        tokens = tokenize(title)
        logger.info(f"[{asin}] brands={brands or '-'} series={series or '-'} model={model or '-'}")

        yt = filter_items(youtube_items, brands, series, model, tokens, ["title"])
        nw = filter_items(news_items, brands, series, model, tokens, ["title"])
        bk = filter_items(books_items, brands, series, model, tokens, ["title", "description"])

        asin_dir = out_root / asin
        asin_dir.mkdir(parents=True, exist_ok=True)
        (asin_dir / "youtube.json").write_text(
            json.dumps({"items": yt}, ensure_ascii=False, indent=2), encoding="utf-8")
        (asin_dir / "news.json").write_text(
            json.dumps({"items": nw}, ensure_ascii=False, indent=2), encoding="utf-8")
        (asin_dir / "books.json").write_text(
            json.dumps({"items": bk}, ensure_ascii=False, indent=2), encoding="utf-8")

        summary.append({"asin": asin, "youtube": len(yt), "news": len(nw), "books": len(bk)})
        logger.info(f"  → youtube={len(yt)} news={len(nw)} books={len(bk)}")

    (out_root / "_summary.json").write_text(
        json.dumps({"items": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Wrote per-ASIN filtered raw for {len(summary)} ASINs")


if __name__ == "__main__":
    main()
