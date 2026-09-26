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
import unicodedata

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

# strict=2 の「裏付け語なし ASIN」経路で、文字種の変わり目で分けた断片の間に
# 挟まってよい最大文字数 (2026-09-24)。「ミュウミュウおすわりぬいぐるみ」が
# 「ミュウミュウのふわふわおすわりぬいぐるみ」に当たる幅 (5) に 1 字の余裕。
CHUNK_PIECE_GAP = 6

# #8162 案B/C (2026-09-24): 裏付け語なし経路 (title_chunks) が有効な ASIN でも、
# ターゲット名そのものが一般語/シリーズ名だと誤りが混ざる (#8122 実測)。
# ASIN の raw プール全体を見て、この経路を有効にするかどうかを ASIN 単位で
# 判定するゲート (allows_title_only_path) の閾値。
# scripts/tests/fixtures/filter_strict2_title_only_labels.jsonl (282件:
# same_product 257 / other_product 20 / unknown 5, #8122 で目視判定) +
# filter_strict2_title_only_pool.jsonl (raw プール) で閾値を振って決定
# (詳細は PR 本文の表)。
#
# TOY_CONTEXT_MIN_RATIO=0.01 単独: same_product 残存 193/257 (75%)、
# other_product 残存 5/20 (25%、#8122 の誤り 20 件を 75% 削減)。案B が
# ほとんどの誤り (スクイッシュ→釣具、スカイチーム→航空連合、KIKKA→BGM 等
# raw プールに「おもちゃの文脈」が皆無なもの) を落とす。
#
# 案C (SERIES_CONTINUATION_MIN_ITEMS) は「ターゲット名の直後に別の語が続く
# 候補の件数」を見るが、ラベル付き集合では人気商品ほど紹介・感想系の
# 動画が多く続き語も増えるため、same_product 側でも 0〜4 件普通に出る
# (最大 4 件: アスレチックランドゲーム)。3 件以上で無効化する案は同時に
# same_product を 193→169 に削るのに other_product は 5→5 のまま (#8122 の
# 残り 5 件 = ナインタイル/あいうえおボード/ふんわりえほん×2ASIN は
# いずれも続き語 0〜2 件で 3 件のラインに届かない) だったため不採用。
# 5 件以上を要求すると、ラベル付き集合内の same_product・other_product
# どちらにも該当が無くなる (no-op) が、より大規模なコーパスでの明確な
# シリーズ ASIN (続き語が多数派になる) への保険として残す。
# 「誤りを機械的にゼロにする」閾値 (ratio>1.0 等) は同時に same_product も
# ゼロにする退化解 (案A自体を無効化するのと同じ) なので不採用。
TOY_CONTEXT_MIN_RATIO = 0.01
SERIES_CONTINUATION_MIN_ITEMS = 5

# #8164 (2026-09-25): 12 字上限撤廃で単一語になった 13 字以上の固有語は、
# それだけで語境界を保った一致があれば strong (model 番号一致や uniq>=2 と
# 同格の強シグナル)。定義は _bounded_in の近くの _long_term_strong 参照。
LONG_TERM_MIN_LEN = 13

# 案B: raw プール中の候補テキストに「おもちゃの文脈」があるとみなす汎用語。
TOY_CONTEXT_GENERIC_WORDS = frozenset({
    "ボードゲーム", "おもちゃ", "知育", "開封", "レビュー", "遊んでみた", "遊んで",
    "紹介", "商品紹介", "知育玩具", "知育おもちゃ", "遊び方", "asmr", "購入品",
    "開封動画",
})

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
_JA_TERM = re.compile(r"[ぁ-んァ-ヶー一-龯]{3,}")
# #8164 以前の抽出 (13 字以上を 12 字 + 残りに割る)。shared の判定を main から
# 縮めないためだけに使う (main() の legacy_df のコメント参照)。
_JA_TERM_LEGACY = re.compile(r"[ぁ-んァ-ヶー一-龯]{3,12}")
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_HIRAGANA_VERB = re.compile(r"^[ぁ-ん]{3,5}[うるく]$")
_TRAIL_NOISE = re.compile(
    r"(\d{1,4}周年(記念)?|記念|限定|新品|BOX|セット|版|号|"
    r"\d{4}年|\d{4}-\d{4}|スペシャル|オリジナル|コレクション)$"
)
_PARTICLE_STRIP = re.compile(r"[もがはにでを]$")


def extract_product_terms(title: str, brands: set, series: set,
                          ja_term: re.Pattern = _JA_TERM) -> set[str]:
    """ASIN タイトルから「商品を一意に同定する」非ブランド・非シリーズ語を抽出。

    例: 「アンパンマン にほんごえいご二語文も！…ことばずかん15周年記念BOX」
        → {ことばずかん, にほんごえいご二語文} 等。
    抽出ルール:
      - 括弧内除去、ブランド/シリーズ語を除去
      - 句読点/スラッシュで分割、長さ 3 以上の日本語語 (かな/カナ/漢字) を拾う
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
        for m in ja_term.finditer(chunk):
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


def _norm(text: str) -> str:
    """全角/半角・大小文字の揺れを吸収 (『ＮＥＷ ＯＨ！』と「NEW OH!」を同一視)。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def _script(ch: str) -> str | None:
    if re.match(r"[ァ-ヶー]", ch):
        return "kata"
    if re.match(r"[一-龯]", ch):
        return "kanji"
    if re.match(r"[ぁ-ん]", ch):
        return "hira"
    if re.match(r"[a-z0-9]", ch):
        return "ascii"
    return None


def _joins_word(a: str, b: str) -> bool:
    """a と b が隣り合うと 1 語として続いて読めるか。ひらがなは助詞
    (の/を/で…) で区切られるのが普通なので続いているとはみなさない。"""
    sa = _script(a)
    return sa is not None and sa != "hira" and sa == _script(b)


def _bounded_in(needle: str, hay: str) -> bool:
    """needle が hay に「別の語の一部としてではなく」現れるか。
    「はじめてのブロック」は「はじめてのブロックワゴン」の中では一致としない
    (同ライン別商品, §4.7)。"""
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        # 直後の "+" は派生モデル名 (やみつきボックス+) の一部として扱う
        if not ((start > 0 and _joins_word(hay[start - 1], needle[0]))
                or (end < len(hay) and (hay[end] == "+"
                                        or _joins_word(needle[-1], hay[end])))):
            return True
        start = hay.find(needle, start + 1)
    return False


def _bounded_in_spaced(needle: str, hay: str) -> bool:
    """_bounded_in と同じだが、hay 側で needle の文字の間に空白が挟まって
    いても一致とする (「北の大地を駆け抜けた　寝台特急カシオペア」
    「まほうのサーティワン アイスクリーム」)。長い固有語 (_long_term_strong)
    専用: 短い語に使うと別々の語をつないで一致させてしまう。"""
    if _bounded_in(needle, hay):
        return True
    pattern = re.compile(r"\s*".join(re.escape(ch) for ch in needle))
    for m in pattern.finditer(hay):
        start, end = m.start(), m.end()
        if not ((start > 0 and _joins_word(hay[start - 1], needle[0]))
                or (end < len(hay) and (hay[end] == "+"
                                        or _joins_word(needle[-1], hay[end])))):
            return True
    return False


def _script_runs(text: str) -> list[str]:
    runs: list[str] = []
    for ch in text:
        if runs and _script(ch) == _script(runs[-1][-1]):
            runs[-1] += ch
        else:
            runs.append(ch)
    return runs


def _chunk_in(chunk: str, hay: str) -> bool:
    """chunk が hay に現れるか。そのままで無ければ、文字種の変わり目で分けた
    断片 (各 2 字以上) が順番どおり CHUNK_PIECE_GAP 字以内の間隔で並ぶものも
    一致とする (例: ミュウミュウ|おすわりぬいぐるみ)。"""
    if _bounded_in(chunk, hay):
        return True
    runs = _script_runs(chunk)
    if len(runs) < 2 or min(len(r) for r in runs) < 2:
        return False
    gap = ".{0,%d}?" % CHUNK_PIECE_GAP
    pattern = re.compile(gap.join(re.escape(r) for r in runs))
    return any(_bounded_in(m.group(0), hay) for m in pattern.finditer(hay))


def title_chunks(title: str) -> list[str]:
    """ターゲットタイトルを括弧除去・句読点分割した断片 (正規化済み)。"""
    clean = re.sub(r"[【\[（\(].*?[】\]）\)]", " ", _norm(title))
    return [c for c in _PUNCT_SPLIT.split(clean) if c.strip()]


# _long_term_strong: 長い語の直後に付く短い版番号・英数字 (2 / DX / 01)。
# 「3歳から」の 3 のように後ろに語が続くものは版番号とみなさない。
_TRAILING_MARKER = re.compile(r"[\s・\-]*([0-9a-z]{1,3})(?=$|[\s・/／()（）【】\[\]])")


# _long_term_strong: 候補側で長い語に直接続く版表記 (４×４ の 4, ゲーム５ の 5)。
_ATTACHED_MARKER = r"([0-9a-z]+)(?![0-9a-zぁ-んァ-ヶー一-龯])"


def compute_title_df(titles_terms: list[tuple[str, set]]) -> dict[str, int]:
    """product_term ごとに、その語を (正規化後の部分文字列として) 含む
    ターゲットタイトルの数を返す (#8164, 2026-09-26)。_long_term_strong の
    兄弟商品判定に使う。shared の判定には使わない (同じ商品の重複出品 =
    長い Amazon 商品名まで数えてしまい、「ゆらりんタワー」「たんぐらむ」を
    shared にして正しい動画を大量に落とす: youtube -90 / news -43)。"""
    norm_titles = [_norm(title) for title, _ in titles_terms]
    vocab: set[str] = set()
    for _, terms in titles_terms:
        vocab |= terms
    return {term: sum(1 for t in norm_titles if _norm(term) in t)
            for term in vocab}


def _long_term_strong(asin_title: str, asin_product_terms: set,
                      shared_terms: set, norm_text: str,
                      title_df: dict | None = None,
                      asin_model: str = "") -> bool:
    """#8164 (2026-09-25): LONG_TERM_MIN_LEN 字以上の固有語 (shared でない)
    が語境界を保って候補に現れれば、model 番号一致や uniq>=2 と同格の強
    シグナルとして単独で strong 扱いにしてよい。

    上限 12 字があった旧 _JA_TERM 実装では、この長さの語が機械的に
    12字+残りの2断片に割れており、両断片が候補に現れれば「unique 2語」を
    満たして strong になっていた（実質1語なのに2countされる偶発的な
    迂回路）。12字上限を撤廃して正しく1語に統合すると uniq=1 になり、
    brand/series/shared の裏付けを新たに要求されて、同一商品の動画/
    ニュースが脱落していた (#8163 の計測: A+B+C の上に重ねると youtube
    net -13 / news net -8)。

    ただし単純に「長い語が1つでも一致すれば strong」にすると、系列名自体が
    13字以上あるケース (ハマクロンコンストラクター, ライジングポリスブレイバー
    等) で、同じ系列の別バリエーション (緊急車両/はたらく車/ファイヤー
    ステーション、白バイ/黒バイ/ZERO) を取り違える (#8164 実測、SHARED_TERM_DF
    (4) に届かない df=2〜3 の系列名は「shared でない」ため素通りしてしまう)。
    そこで、長い語より**後ろ**にある他の strong 語 (型番・色・セット名などの
    識別サフィックス) がタイトルにあれば、それも候補に現れることを要求する。
    長い語より**前**にある strong 語 (メガハウス・アーテック等、KNOWN_BRANDS
    未登録のメーカー名) は要求しない (レビュー動画では省略されがちで、
    誤マッチの原因にもならないため)。asin_title が空 (呼び出し側が位置を
    渡せない) ときは安全側に倒し、全ての strong 語を要求する。

    語境界チェック (_bounded_in, #8122 と同じ: 前後にカタカナ/漢字/英数字が
    続いたら不一致、直後の "+" は派生モデルとして不一致) は長い語・
    識別サフィックスの両方に適用する。長い語は、途中に空白が挟まった表記も
    一致とする (_bounded_in_spaced)。

    後置の識別語には、strong 語 (4 字以上) にならない短い版番号・英数字
    (「…セレクション2」「… DX」「… 01」, _TRAILING_MARKER) と、後ろに付く
    KNOWN_BRANDS/SERIES (キャラクター違い: 「はじめてのマナー豆おおつぶ
    すみっコぐらし」に「… ドラえもん」) も含める (#8164, 2026-09-26)。

    title_df (compute_title_df) で長い語が他のターゲットタイトルにも
    現れる (カタログに兄弟商品がある) ときは、その語は商品名ではなく
    系列名なので、後置の識別語が 1 つ以上あることを要求する。識別語の無い
    無印・基本セット (「どこでもドラえもん日本旅行ゲーム ミニ」の「ミニ」は
    識別語にならない) に兄弟商品 (…ゲーム5) の動画を通さない。ただし
    ターゲット側で長い語の直後に語 (「ミニ」) があれば、それを必須語として
    扱う (「…ゲーム６＋…ゲーム ミニ」はミニの動画として残す、2026-09-26)。

    候補側で長い語の直後に英数字が直接続く (「クリスタルルービックキューブ
    ４×４」「…日本旅行ゲーム５」) のは版違いの表記なので、ターゲット側の
    同じ位置に同じ英数字が無ければ一致としない。候補に長い語が複数回
    現れるときは、どれか 1 つがこの条件を満たせばよい。「やきたてパン工場35万個
    突破」のように後ろに漢字・かなが続く数量表現は除く。

    ASIN に型番があるときは、候補の型番が別物 (71439 に 71440) なら
    この経路を使わない。"""
    long_terms = [pt for pt in asin_product_terms
                  if len(pt) >= LONG_TERM_MIN_LEN and pt not in shared_terms]
    if not long_terms:
        return False
    if asin_model:
        cand_model = extract_model_number(norm_text.upper())
        if cand_model and cand_model.upper() != asin_model.upper():
            return False
    strong_terms = {pt for pt in asin_product_terms
                    if len(pt) >= STRONG_TERM_MIN_LEN and pt not in shared_terms}
    norm_title = _norm(asin_title)
    title_brands, title_series = extract_brand_series(asin_title)
    for lt in long_terms:
        nlt = _norm(lt)
        if not _bounded_in_spaced(nlt, norm_text):
            continue
        idx = norm_title.find(nlt) if norm_title else -1
        if idx < 0:
            required = {_norm(t) for t in strong_terms - {lt}}
        else:
            lt_end = idx + len(nlt)
            required = {_norm(t) for t in strong_terms
                        if t != lt and norm_title.find(_norm(t), lt_end) >= 0}
            required |= {_norm(w) for w in title_brands | title_series
                         if norm_title.find(_norm(w), lt_end) >= 0}
            marker = _TRAILING_MARKER.match(norm_title, lt_end)
            if marker:
                required.add(marker.group(1))
        if title_df is not None and title_df.get(lt, 1) >= 2 and not required:
            nxt = re.match(r"\s*(\S{2,})", norm_title[lt_end:]) if idx >= 0 else None
            if not nxt:
                continue
            required = {nxt.group(1)}
        own = re.match(r"\s*([0-9a-z]+)", norm_title[idx + len(nlt):]) if idx >= 0 else None
        own_marker = own.group(1) if own else None
        occurrences = [m.end() for m in re.finditer(re.escape(nlt), norm_text)]
        if occurrences and not any(
                (a := re.match(_ATTACHED_MARKER, norm_text[end:])) is None
                or a.group(1) == own_marker
                for end in occurrences):
            continue
        if all(_bounded_in(t, norm_text) for t in required):
            return True
    return False


_TRAIL_CONT_STRIP = re.compile(
    r"^[\s「」『』（）()\[\]【】・:：\-—_/／!！?？。、,，.]*")


def _amazon_title_context_terms(amazon_title: str, chunks: list[str]) -> set[str]:
    """#8162 案B: per_asin/<ASIN>/amazon.json の正式名から、ターゲット名
    (chunks) 自体とは別の「おもちゃの文脈」語 (ブランド/シリーズ/固有語) を
    抽出する。ターゲット名の部分文字列 (「Time to Time」の "Time" 等) は
    除外する (それ自体は商品を裏付けない)。"""
    if not amazon_title:
        return set()
    brands, series = extract_brand_series(amazon_title)
    terms = extract_product_terms(amazon_title, brands, series) | brands | series
    out = set()
    for t in terms:
        nt = _norm(t)
        if len(nt) < 2:
            continue
        if any(nt in c or c in nt for c in chunks):
            continue
        out.add(nt)
    return out


def _toy_context_hit(norm_text: str, extra_context_terms: set[str]) -> bool:
    for b in KNOWN_BRANDS:
        if _norm(b) in norm_text:
            return True
    for t in extra_context_terms:
        if t in norm_text:
            return True
    for w in TOY_CONTEXT_GENERIC_WORDS:
        if _norm(w) in norm_text:
            return True
    return False


# 助詞始まり (を紹介/で遊んでみた/の使い方…) は「同じ商品についての実況・
# 感想文」の続きであってシリーズ別タイトルの証拠にならないので除外する。
_TRAIL_PARTICLE_LEAD = re.compile(r"^[をでにとがはもへや]")


def _trailing_continuation(chunk: str, norm_text: str) -> str | None:
    """chunk の直後 (句読点・括弧を挟んでよい) に続く、別の商品名らしき語を返す。
    汎用文脈語 (TOY_CONTEXT_GENERIC_WORDS) / PRODUCT_NOISE / 助詞始まりの
    続き (「を紹介」「で遊んでみた」等、同じ商品についての実況・感想の
    続きであってシリーズ別タイトルの証拠にならない) は None。"""
    idx = norm_text.find(chunk)
    if idx == -1:
        return None
    rest = _TRAIL_CONT_STRIP.sub("", norm_text[idx + len(chunk):])
    m = _JA_TERM.match(rest) or re.match(r"[a-z][a-z0-9]{2,}", rest)
    if not m:
        return None
    cont = m.group(0)
    if cont in {_norm(w) for w in TOY_CONTEXT_GENERIC_WORDS} or cont in PRODUCT_NOISE:
        return None
    if _TRAIL_PARTICLE_LEAD.match(cont):
        return None
    return cont


def _matching_pool_texts(chunks: list[str], pool_texts: list[str]) -> list[str]:
    out = []
    for text in pool_texts:
        norm = _norm(text)
        if all(_chunk_in(c, norm) for c in chunks):
            out.append(norm)
    return out


def allows_title_only_path(chunks: list[str], pool_texts: list[str],
                           amazon_title: str = "") -> bool:
    """#8162 案B/C: 裏付け語なし経路 (title_chunks) を ASIN 単位で有効にして
    よいかを、ASIN の raw プール全体 (pool_texts = yt_pool の title 群) で判定する。

    案B (一般語判定): chunks (ターゲット名) を含む候補のうち、ブランド名 /
    per_asin/amazon.json 正式名の語 (target 自体は除く) / 汎用文脈語
    (「ボードゲーム」「知育」等) のいずれかを持つ割合が TOY_CONTEXT_MIN_RATIO
    未満なら無効 (例: スクイッシュ→釣具、スカイチーム→航空連合、KIKKA→BGM は
    候補全体がゼロ)。

    案C (シリーズ名判定): chunks の直後に (汎用文脈語ではない) 別の語が続く
    候補が SERIES_CONTINUATION_MIN_ITEMS 件以上あるなら、ターゲット名はシリーズ
    名だけの可能性が高いとみなし無効 (例: 「ふんわりえほん こぐまくんの…」
    「ナインタイル ポケモンドコダ」)。

    閾値の根拠は TOY_CONTEXT_MIN_RATIO / SERIES_CONTINUATION_MIN_ITEMS の定義
    コメントと PR 本文 (#8162 段階3) の表を参照。"""
    if not chunks:
        return False
    matching = _matching_pool_texts(chunks, pool_texts)
    if not matching:
        return False
    extra_context_terms = _amazon_title_context_terms(amazon_title, chunks)
    hits = sum(1 for t in matching if _toy_context_hit(t, extra_context_terms))
    if (hits / len(matching)) < TOY_CONTEXT_MIN_RATIO:
        return False
    continuations = sum(1 for t in matching
                        if any(_trailing_continuation(c, t) for c in chunks))
    if continuations >= SERIES_CONTINUATION_MIN_ITEMS:
        return False
    return True


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
                 shared_terms: set | None = None,
                 asin_title: str = "",
                 amazon_title: str = "",
                 allow_long_term: bool = True,
                 enable_title_chunks: bool = True,
                 title_df: dict | None = None) -> list:
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

    裏付け語なし ASIN (strict=2, 2026-09-24 追加): 上の条件だと、ターゲット
    タイトルが「きらめく財宝」のように商品名 1 語だけの ASIN は unique 1 語を
    裏付ける brand/series/shared がタイトルに存在せず、正しい動画も一律に
    落ちていた。そこで asin_title が渡され、ASIN に brand/series/model/
    shared (4 字以上) が一つも無く unique が 1 語以上ある場合に限り、
    「タイトルの断片 (title_chunks) が全部、別の語の一部としてではなく
    候補に現れる」ことを strong とする。断片を全部要求するので
    「エヴァンゲリオンプライム2号機」に初号機の動画、「25音 カラフル鉄琴」に
    20音の動画は通らない。語境界を見るので「はじめてのブロック」に
    「はじめてのブロックワゴン」も通らない。

    この経路だけで strong になった項目には `_match: "title_only"` を付ける
    (#8162 案 A)。ターゲット名そのものが一般語の ASIN (スクイッシュ/釣具 等)
    では誤りが混ざることが実測されており (#8122 で 282 件中 20 件別商品)、
    公開記事への自動埋め込み (build_post._fallback_youtube_embeds) と
    体験談抽出 (mine_experience)・Jules への sources 提供 (build_jules_prompt)
    では除外する。既存の strong 経路 (brand/series/shared に裏付けられた一致)
    の項目には付けない。

    さらに ASIN 単位で allows_title_only_path() のゲートを通らないと
    この経路自体を無効にする (#8162 案B/C, 2026-09-24)。amazon_title は
    per_asin/<ASIN>/amazon.json の正式名 (無ければ空文字、ゲートは raw
    プールの文脈語判定のみで行う)。

    長い固有語の単独 strong 化 (#8164, 2026-09-25): LONG_TERM_MIN_LEN
    (13) 字以上の unique な product_term が、語境界を保って候補に現れれば
    strong とする (`_long_term_strong`)。title_chunks 経路と違い
    brand/series/model の有無を問わず常に評価する (12字上限撤廃の副作用で
    uniq=1 に落ちた既存の正しい一致を救うための経路であり、「裏付け語なし
    ASIN」限定ではない)。長い語より後ろにある他の strong 語 (型番/色/
    セット名) は同じ系列の別バリエーションとの取り違え防止のため追加で
    要求する (詳細は `_long_term_strong` のコメント)。誤マッチ抑制のため
    shared_terms には該当しないことを要求する。この経路で strong になった
    項目には `_match: "title_only"` を付けない (title_chunks 経路と違い、
    高い特異性を持つ語＋識別サフィックスの語境界一致そのものが十分な裏付け
    であり、ターゲット名が一般語かどうかのゲート (allows_title_only_path)
    の対象外)。allow_long_term=False で呼び出し側から無効化できる。

    asin_title は「裏付け語なし ASIN」の title_chunks 経路 (上記) だけでなく
    長い固有語の位置判定 (`_long_term_strong`) にも使うため、news でも渡す。
    ただし title_chunks 経路自体は news で誤りが多いこと (#8122: 93件中36件
    別商品) が分かっているので、enable_title_chunks=False で独立に無効化
    できる。
    """
    has_strong_anchor = bool(asin_model or asin_product_terms or asin_series)
    shared_set = shared_terms or set()
    strong_terms = {pt for pt in asin_product_terms
                    if len(pt) >= STRONG_TERM_MIN_LEN}
    chunks: list[str] = []
    if (strict >= 2 and asin_title and enable_title_chunks
            and not (asin_brands or asin_series or asin_model)
            and strong_terms and not (strong_terms & shared_set)):
        candidate_chunks = title_chunks(asin_title)
        if candidate_chunks:
            pool_texts = [" ".join(str(it.get(k, "")) for k in text_keys)
                         for it in raw_items if isinstance(it, dict)]
            if allows_title_only_path(candidate_chunks, pool_texts, amazon_title):
                chunks = candidate_chunks
    scored = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(k, "")) for k in text_keys)
        s, signals = score_item(text, asin_brands, asin_series, asin_model,
                                asin_tokens, asin_product_terms,
                                shared_terms=shared_terms)
        title_only = False
        if strict >= 2:
            uniq = signals.get("product_term_unique", 0)
            shared = signals.get("product_term_shared", 0)
            strong = ((signals.get("model") and signals.get("brand"))
                      or uniq >= 2
                      or (uniq == 1 and (signals.get("brand")
                                         or signals.get("series")
                                         or shared >= 1)))
            if not strong:
                norm_text = _norm(text)
                if allow_long_term and _long_term_strong(
                        asin_title, asin_product_terms, shared_set, norm_text,
                        title_df=title_df, asin_model=asin_model):
                    strong = True
                elif chunks and all(_chunk_in(c, norm_text) for c in chunks):
                    strong = True
                    title_only = True
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
            if title_only:
                enriched["_match"] = "title_only"
            scored.append(enriched)
    scored.sort(key=lambda x: x["_relevance_score"], reverse=True)
    return scored[:top_n]


def exclude_title_only(payload):
    """``_match: "title_only"`` (#8162 案 A) が付いた項目を除いて返す。

    filter_items の裏付け語なし経路 (タイトル断片一致のみ) で strong に
    なった項目は、ターゲット名そのものが一般語の ASIN (スクイッシュ→釣具、
    スカイチーム→航空連合 等) で誤りが混ざることが実測されている
    (#8122: 282 件中 20 件が別商品)。公開記事への自動埋め込み
    (build_post._fallback_youtube_embeds)・体験談抽出
    (mine_experience.gather_youtube_opportunistic)・Jules への sources 提供
    (build_jules_prompt) の 3 消費側は、この共通ヘルパーで除外してから使う。

    ``{"items": [...]}`` 形と裸の list、どちらも受け付ける。"""
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            filtered = [it for it in items
                        if not (isinstance(it, dict) and it.get("_match") == "title_only")]
            return {**payload, "items": filtered}
        return payload
    if isinstance(payload, list):
        return [it for it in payload
                if not (isinstance(it, dict) and it.get("_match") == "title_only")]
    return payload


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


def load_per_asin_amazon_title(raw_dir: pathlib.Path, asin: str) -> str:
    """data/raw/per_asin/<ASIN>/amazon.json (楽天ランキング由来の新規 ASIN 用
    キャッシュ、build_jules_prompt._amazon_item と同じ形) から正式名を返す。
    無ければ空文字 (#8162 案B の文脈語判定用)。"""
    p = raw_dir / "per_asin" / asin / "amazon.json"
    if not p.exists():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    item = d.get("item") if isinstance(d, dict) else None
    if isinstance(item, dict):
        return item.get("title", "") or ""
    return ""


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
    legacy_df: dict[str, int] = {}
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
        for t in extract_product_terms(title, brands, series, _JA_TERM_LEGACY):
            legacy_df[t] = legacy_df.get(t, 0) + 1
    # #8164: 12 字上限の撤廃で df の数え方が変わり、「パソコン」「トレイン」が
    # shared から外れた (旧抽出では長い語を割った断片が偶然 df を稼いでいた)。
    # するとブランド一致 + unique 1 語で同ブランドの別モデル (すみっコぐらし
    # パソコン MY LIVE ← プレミアムプラス/Phone) が通る。この PR の影響を
    # 長い固有語の経路に限るため、旧抽出の df でも閾値以上なら shared とする
    # (shared の集合を main から縮めない)。カテゴリ語が複合語の中にあると
    # 数えられない (トレインは 24 タイトルに出るのに df=3) こと自体は別件。
    shared_terms = {t for t in term_df
                    if max(term_df[t], legacy_df.get(t, 0)) >= SHARED_TERM_DF}
    title_df = compute_title_df([(e[0], e[5]) for e in extracted.values()])
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
                          strict=2, shared_terms=shared_terms,
                          asin_title=title,
                          amazon_title=load_per_asin_amazon_title(raw_dir, asin),
                          allow_long_term=True, enable_title_chunks=True,
                          title_df=title_df)
        # news には裏付け語なし経路 (title_chunks, enable_title_chunks=False) を
        # 使わない。2026-09-24 の実測で追加分 93 件中 36 件が別商品
        # (「ネムリラ コードレス HR」新発売のような派生モデルの告知・
        # 同名の航空連合/釣具) だった。youtube は 282 件中 20 件。
        # 長い固有語の単独 strong 化 (allow_long_term, #8164) は asin_title が
        # 位置判定に要るため news にも渡す。2026-09-26 の実測 (main 比) で
        # news は追加 4 / 脱落 1 (追加は全て同一商品、脱落は別商品の除去)。
        nw = filter_items(nw_pool, brands, series, model, tokens,
                          product_terms, ["title"], strict=2,
                          shared_terms=shared_terms,
                          asin_title=title, allow_long_term=True,
                          enable_title_chunks=False, title_df=title_df)
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
