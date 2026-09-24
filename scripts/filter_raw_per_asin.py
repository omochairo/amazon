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

import _fetch_targets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("filter_raw_per_asin")

SCORE_THRESHOLD = 3.0

# strict 判定で product_term ヒットとして数える最小文字数 (§4.7, 2026-07-07)。
# 3 文字の汎用語 (例: バケツ) は「よくばりバケツ」と「のりものブロックバケツ」を
# 区別できず同ブランド別商品を通してしまうため、商品同定シグナルには使わない。
STRONG_TERM_MIN_LEN = 4

# この件数以上のターゲット ASIN タイトルに出現する product_term は「共有語」
# (未登録ブランド名/商品ライン名/カテゴリ語: トイローヤル, GraviTrax, ブロック等)
# とみなし、単独では商品同定シグナルにしない (§4.7)。KNOWN_BRANDS の手動辞書では
# 追いつかないため、ターゲット集合 (~1500 タイトル) から動的に判定する。
SHARED_TERM_DF = 4

# コンテンツ種別ごとの top-N。youtube は本文に iframe で 1 件ずつ縦に並ぶので
# 多すぎると記事が重くなる + 関連度の薄い候補を巻き込みやすいため少なめ。
TOP_N_DEFAULT = 5
TOP_N_BY_KEY = {"youtube": 3}

# 既知ブランド/シリーズ辞書 (拡張可)
KNOWN_BRANDS = [
    "レゴ", "LEGO", "プラレール", "トミカ", "アンパンマン", "ディズニー", "サンリオ",
    "ポケモン", "すみっコぐらし", "リカちゃん", "シルバニアファミリー", "BorneLund",
    "ボーネルンド", "くもん", "公文", "学研", "ピープル", "バンダイ", "タカラトミー",
    "セガトイズ", "セガフェイブ", "エポック", "アガツマ", "ジョイレア", "Joyreal",
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

# product_term 抽出時のサフィックス/汎用語ノイズ。これらは商品固有性が無いので
# 「これだけ一致しても商品同定にならない」語。
PRODUCT_NOISE = NOISE | {
    "周年", "記念", "限定", "シリーズ", "セット", "セレクション", "スペシャル",
    "バージョン", "オリジナル", "コレクション", "デラックス", "プレミアム",
    "スタンダード", "ベーシック", "保護", "対象", "対応", "推奨", "簡単", "新品",
    "中古", "大容量", "完全", "公式", "サイズ", "本体", "付属", "別売",
}


_MODEL_PATTERNS = [
    re.compile(r"\b(\d{5})\b"),                           # LEGO 71439 など
    re.compile(r"\b(\d{4})\b"),                           # 4 桁
    re.compile(r"(?:No\.?|NO\.?)\s*(\d{1,4})", re.I),     # トミカ No.94
    re.compile(r"\b([A-Z]{1,2}-?\d{1,4}[A-Z]?)\b"),       # S-07 / C-12 / M55
]


def extract_model_number(text: str) -> str:
    """LEGO/トミカ系のモデル番号を抽出。5桁優先、なければ4桁→No.XX→英数字混在の順。
    最初にヒットしたものを返す。"""
    if not text:
        return ""
    for pat in _MODEL_PATTERNS:
        m = pat.search(text)
        if m:
            v = m.group(1)
            # 単独の "OK"/"NEW" などを除外
            if len(v) >= 2 and v.upper() not in {"OK", "NEW", "BOX", "DX"}:
                return v
    return ""


_PUNCT_SPLIT = re.compile(r"[\s！。、・/／,!\?？:：「」『』\-]+")
_JA_TERM = re.compile(r"[ぁ-んァ-ヶー一-龯]{3,12}")
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_HIRAGANA_VERB = re.compile(r"^[ぁ-ん]{3,5}[うるく]$")
_TRAIL_NOISE = re.compile(
    r"(\d{1,4}周年(記念)?|記念|限定|新品|BOX|セット|版|号|"
    r"\d{4}年|\d{4}-\d{4}|スペシャル|オリジナル|コレクション)$"
)
_PARTICLE_STRIP = re.compile(r"[もがはにでを]$")


def extract_product_terms(title: str, brands: set, series: set) -> set[str]:
    """ASIN タイトルから「商品を一意に同定する」非ブランド・非シリーズ語を抽出。

    例: 「アンパンマン にほんごえいご二語文も！…ことばずかん15周年記念BOX」
        → {ことばずかん, にほんごえいご二語文} 等。
    抽出ルール:
      - 括弧内除去、ブランド/シリーズ語を除去
      - 句読点/スラッシュで分割、長さ 3-12 の日本語語 (かな/カナ/漢字) を拾う
      - 末尾の 周年記念BOX / 限定 / 年号 等のサフィックスを剥がして core を残す
      - 末尾の 1文字助詞 (もがはにでを) を安全に strip
      - 動詞風 (5字以下のひらがな末尾 う/る/く) は除外
      - ASCII モデル風 (例 M55) は別途模型番号で扱うのでここでは英字 3+ のみ拾う
    """
    if not title:
        return set()
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", title)
    for w in (brands | series):
        clean = clean.replace(w, " ")
    terms: set[str] = set()
    for chunk in _PUNCT_SPLIT.split(clean):
        chunk = chunk.strip()
        if not chunk:
            continue
        for m in _JA_TERM.finditer(chunk):
            t = m.group(0)
            # 末尾サフィックスを最大 2 回剥がす (例: "15周年記念BOX" → "")
            for _ in range(2):
                stripped = _TRAIL_NOISE.sub("", t)
                if stripped == t:
                    break
                t = stripped
            # 末尾の助詞を1文字剥がす (例: "にほんごえいご二語文も" → "にほんごえいご二語文")
            stripped_particle = _PARTICLE_STRIP.sub("", t)
            if stripped_particle != t:
                if len(stripped_particle) >= 3:
                    t = stripped_particle
            if len(t) < 3:
                continue
            if t in PRODUCT_NOISE:
                continue
            if _HIRAGANA_VERB.match(t):
                continue
            terms.add(t)
        for m in _ASCII_TERM.finditer(chunk):
            t = m.group(0)
            if t.upper() in {"NEW", "SET", "FOR", "THE", "BOX", "SEGA",
                             "FAVE", "TOMY", "LTD", "VER"}:
                continue
            terms.add(t)
    return terms


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
               asin_model: str, asin_tokens: set,
               asin_product_terms: set,
               shared_terms: set | None = None) -> tuple[float, dict]:
    """1 アイテムのテキストに対する関連度スコアと、どのシグナルが当たったかを返す。

    signals = {brand, series, model, product_term, ..., token_overlap} の bool/int。
    呼び出し側 (filter_items) が strict モードで「ブランドだけ」を弾く判断に使う。
    product_term_unique/shared は STRONG_TERM_MIN_LEN 以上の語のみ数え、
    shared_terms (ターゲット横断で頻出する共有語) かどうかで分けて数える。
    """
    if not item_text:
        return 0.0, {}
    shared_terms = shared_terms or set()
    score = 0.0
    signals: dict = {"brand": False, "series": False, "model": False,
                     "product_term": 0, "product_term_unique": 0,
                     "product_term_shared": 0, "token_overlap": 0}
    item_brands, item_series = extract_brand_series(item_text)
    if asin_brands & item_brands:
        score += 5.0
        signals["brand"] = True
    if asin_model and asin_model in item_text:
        score += 10.0
        signals["model"] = True
    if asin_series & item_series:
        score += 3.0
        signals["series"] = True
    if asin_product_terms:
        hits = sum(1 for pt in asin_product_terms if pt in item_text)
        if hits:
            score += min(hits * 4.0, 8.0)
            signals["product_term"] = hits
        for pt in asin_product_terms:
            if len(pt) < STRONG_TERM_MIN_LEN or pt not in item_text:
                continue
            key = ("product_term_shared" if pt in shared_terms
                   else "product_term_unique")
            signals[key] += 1
    item_tokens = tokenize(item_text)
    overlap = len(asin_tokens & item_tokens)
    if overlap:
        score += min(overlap * 0.5, 3.0)
        signals["token_overlap"] = overlap
    return score, signals


def filter_items(raw_items: list, asin_brands: set, asin_series: set,
                 asin_model: str, asin_tokens: set,
                 asin_product_terms: set,
                 text_keys: list[str],
                 top_n: int = TOP_N_DEFAULT,
                 strict: int = 0,
                 shared_terms: set | None = None) -> list:
    """raw 配列をスコアリングして閾値以上を返す。

    strict=1 (books, 旧判定): ASIN にアンカー (model/terms/series) がある場合、
    series / product_term (文字数不問) / model+brand のいずれかを要求。
    アンカーが無い ASIN は素通し (ブランド一致のみで通る)。

    strict=2 (youtube/news, §4.7 2026-07-07 改訂): 候補は「商品を同定する」
    強シグナルを満たさないと除外する。product_term は
    STRONG_TERM_MIN_LEN 以上の語のみ数え、unique (この商品固有) と
    shared (ターゲット横断頻出 = ブランド/ライン/カテゴリ語) を区別する:
      - model 一致 **かつ** 同じ候補に ASIN のブランドも一致、または
      - unique 2 語以上、または
      - unique 1 語 **かつ** (brand / series / shared 1 語以上)

    shared のみの組合せ (例: ナノブロック+ポケットモンスター) は strong に
    しない: franchise+カテゴリ対が同シリーズ別商品 (リザードン記事に
    カビゴン動画) を通すことを実測確認したため。unique が抽出できない
    商品 (例: GraviTrax スターターセット) は空リストになるが許容する。

    旧判定の穴 (2026-07-07 に本番記事で実測):
      - series 単独 / series+brand を strong 扱い → 同シリーズ別商品が混入
      - product_term に文字数下限なし → 「バケツ」等の汎用 3 文字語の一致だけで
        別商品 (よくばりバケツ vs のりものブロックバケツ) が通過
      - 1 語一致のみで strong → 商品ライン名 (GraviTrax) や カテゴリ語
        (ブロック) だけ一致する別商品の動画/セール記事が通過
      - アンカー (model/terms/series) が抽出できない ASIN は strict 自体を
        バイパス → ブランド一致 +5.0 だけで閾値超え
    誤マッチを載せるより空リストの方が良い (記事側は動画/ニュース無しで成立)
    ため、strict=2 ではアンカー無し ASIN の素通しも廃止した。
    """
    has_strong_anchor = bool(asin_model or asin_product_terms or asin_series)
    scored = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(k, "")) for k in text_keys)
        s, signals = score_item(text, asin_brands, asin_series, asin_model,
                                asin_tokens, asin_product_terms,
                                shared_terms=shared_terms)
        if strict >= 2:
            uniq = signals.get("product_term_unique", 0)
            shared = signals.get("product_term_shared", 0)
            strong = ((signals.get("model") and signals.get("brand"))
                      or uniq >= 2
                      or (uniq == 1 and (signals.get("brand")
                                         or signals.get("series")
                                         or shared >= 1)))
            if not strong:
                continue
        elif strict and has_strong_anchor:
            strong = (signals.get("series") or signals.get("product_term")
                      or (signals.get("model") and signals.get("brand")))
            if not strong:
                continue
        if s >= SCORE_THRESHOLD:
            enriched = dict(item)
            enriched["_relevance_score"] = round(s, 2)
            scored.append(enriched)
    scored.sort(key=lambda x: x["_relevance_score"], reverse=True)
    return scored[:top_n]


def collect_targets(amazon_items: list, articles_dir: pathlib.Path) -> dict:
    """処理対象 ASIN を {asin: title} で集める。
    1) amazon.json の全 ASIN (最新の検索結果)
    2) data/articles/ にある全記事の ASIN (過去 Jules セッションが選んだもの)
       → amazon.json から消えても per_asin/ を維持し、Jules がクローンした
         古い main で参照されたときに空にならないようにする。
    """
    targets: dict[str, str] = {}
    for amz in amazon_items:
        asin = amz.get("asin", "")
        if asin:
            targets[asin] = amz.get("title", "")
    if not articles_dir.exists():
        return targets
    for art_path in articles_dir.glob("*.json"):
        # ファイル名規則: YYYY-MM-DD-ASIN.json
        stem = art_path.stem
        parts = stem.split("-")
        if len(parts) < 4:
            continue
        asin = parts[-1]
        if asin in targets:
            continue  # amazon.json 由来を優先
        try:
            art = json.loads(art_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # title は記事 JSON の product.name or title フィールド
        title = ""
        if isinstance(art.get("product"), dict):
            title = art["product"].get("name", "") or art["product"].get("title", "")
        if not title:
            title = art.get("title", "")
        if title:
            targets[asin] = title
    return targets


def main():
    root = pathlib.Path(".")
    raw_dir = root / "data" / "raw"
    articles_dir = root / "data" / "articles"
    amazon_path = raw_dir / "amazon.json"
    if not amazon_path.exists():
        logger.error(f"{amazon_path} not found")
        return

    amazon = json.loads(amazon_path.read_text(encoding="utf-8"))
    amazon_items = amazon.get("items", [])
    if not amazon_items:
        logger.warning("amazon.json has no items (still processing existing articles)")

    targets = collect_targets(amazon_items, articles_dir)
    if not targets:
        logger.warning("No targets to process")
        return
    logger.info(f"Targets: {len(amazon_items)} from amazon.json + {len(targets) - len(amazon_items)} from articles/")

    youtube_items = json.loads((raw_dir / "youtube.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "youtube.json").exists() else []
    news_items = json.loads((raw_dir / "news.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "news.json").exists() else []
    books_items = json.loads((raw_dir / "books_result.json").read_text(encoding="utf-8")).get("items", []) \
        if (raw_dir / "books_result.json").exists() else []

    logger.info(f"Source pools: youtube={len(youtube_items)} news={len(news_items)} books={len(books_items)}")

    out_root = raw_dir / "per_asin"
    out_root.mkdir(parents=True, exist_ok=True)

    def _dedupe_by_url(lst: list) -> list:
        seen = set()
        out = []
        for it in lst:
            if not isinstance(it, dict):
                continue
            u = it.get("url") or it.get("link") or ""
            if u and u in seen:
                continue
            if u:
                seen.add(u)
            out.append(it)
        return out

    # §4.7: 全ターゲットの product_term の doc frequency を先に集計し、
    # SHARED_TERM_DF 件以上のタイトルに出る語を「共有語」(単独では商品を
    # 同定しない語) として filter_items に渡す。
    extracted: dict[str, tuple] = {}
    term_df: dict[str, int] = {}
    for asin, title in targets.items():
        if not asin:
            continue
        brands, series = extract_brand_series(title)
        model = extract_model_number(title)
        tokens = tokenize(title)
        product_terms = extract_product_terms(title, brands, series)
        extracted[asin] = (title, brands, series, model, tokens, product_terms)
        for t in product_terms:
            term_df[t] = term_df.get(t, 0) + 1
    shared_terms = {t for t, n in term_df.items() if n >= SHARED_TERM_DF}
    logger.info(f"Shared terms (df>={SHARED_TERM_DF}): {len(shared_terms)}")

    summary = []
    for asin, (title, brands, series, model, tokens, product_terms) in extracted.items():
        logger.info(
            f"[{asin}] brands={brands or '-'} series={series or '-'} "
            f"model={model or '-'} product_terms={product_terms or '-'}"
        )

        # Pool = global (genre) + per-ASIN raw (stale-first 巡回で蓄積)。
        # per-ASIN raw は当該 ASIN だけを狙った fetch 結果なので関連度が高い。
        # global pool は genre 全般なのでブランド被りで偶然 hit するケース用。
        # 両方を union-dedup してから strict filter に渡す。
        yt_pool = _dedupe_by_url(youtube_items +
                                 _fetch_targets.load_per_asin_raw_items(raw_dir, "youtube", asin))
        nw_pool = _dedupe_by_url(news_items +
                                 _fetch_targets.load_per_asin_raw_items(raw_dir, "news", asin))
        bk_pool = _dedupe_by_url(books_items +
                                 _fetch_targets.load_per_asin_raw_items(raw_dir, "books", asin))

        # youtube / news は §4.7 の商品同定判定 (strict=2)。
        # books は関連読み物なので旧判定 (strict=1) のまま (§4.7 の指摘対象外)。
        yt = filter_items(yt_pool, brands, series, model, tokens,
                          product_terms, ["title"],
                          top_n=TOP_N_BY_KEY.get("youtube", TOP_N_DEFAULT),
                          strict=2, shared_terms=shared_terms)
        nw = filter_items(nw_pool, brands, series, model, tokens,
                          product_terms, ["title"], strict=2,
                          shared_terms=shared_terms)
        bk = filter_items(bk_pool, brands, series, model, tokens,
                          product_terms, ["title", "description"],
                          strict=1, shared_terms=shared_terms)

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
