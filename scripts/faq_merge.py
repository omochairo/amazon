"""記事 JSON の ``faq`` と SEO サイドカーの ``faq_extended`` を 1 本にまとめる。

経緯: build_post.py は ``faq_extended or faq`` を表示と JSON-LD (FAQPage) に使っていたため、
サイドカーがある記事では記事本体の ``faq`` が一切ページに出なかった (omochairo/amazon#11865 で
発覚)。ここでは両方を合わせ、同じ質問・ほぼ同じ質問を落として上限で切る。

順序: ``faq_extended`` (需要クエリで作った 6 問以内) を先に、記事本体の ``faq`` のうち
重複しないものを後ろに足す。既存ページで先頭に見えていた Q&A の並びは変わらない。

重複の判定 (どれかに当たれば後から来た方を落とす):
  照合先は faq_extended だけ。faq_extended の無い記事の faq はこれまで通りそのまま出す。
  価格・在庫など動的事実を含む記事側の Q&A は、faq_extended のある記事では足さない。
  1. 正規化後の質問が一致 (NFKC・空白と記号を除去)
  2. 商品名の語を除いた質問の文字 2-gram の Jaccard が ``SIMILARITY_THRESHOLD`` 以上
  3. 質問の意図が同じ (``_INTENT_PATTERNS``。「何歳から」「どこで買える」のように
     同じ意図を言い換えただけの定型の質問)
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# 表示する FAQ の上限。faq_extended 単体の上限 (generate_faq_seo.MAX_FAQ_ITEMS = 6) の
# 上に、記事本体の faq (多くは 3 問前後) を足した分。
MAX_MERGED_FAQ_ITEMS = 9

SIMILARITY_THRESHOLD = 0.6
# 商品名の語が助詞に付いて取り切れないとき用 (「星月夜(21333)のピース数」など)。残りの文字列どうしの一致率。
SEQUENCE_THRESHOLD = 0.75

# 同じ意図の言い換えを束ねる。広げすぎると別の質問を落とすので、実データで言い換えが
# 多かった定型だけに絞る。
_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("age", re.compile(r"何歳から|何歳ですか|何歳でも|何ヶ月から|対象年齢|いつから(遊|使)")),
    ("where_to_buy", re.compile(r"どこで(買|購入|売|手に)|購入(でき|方法|場所)|買える")),
    # 電池の交換方法・持ち・充電時間は別の質問なので束ねない (_INTENT_EXCLUDE)
    ("battery", re.compile(r"電池|バッテリー")),
    ("release_date", re.compile(r"発売日")),
    ("list_price", re.compile(r"定価")),
    ("safety", re.compile(r"安全")),
    ("effect", re.compile(r"知育効果|身につく|鍛え")),
    ("contents", re.compile(r"セット内容|内容品|付属品|何が入|何が含まれ")),
    ("compat", re.compile(r"互換")),
)

# この語を含む質問は、その意図には束ねない (別の質問として残す)。
_INTENT_EXCLUDE: dict[str, re.Pattern[str]] = {
    "battery": re.compile(r"交換|入れ替|入れ方|持ち|時間|寿命|充電"),
    "age": re.compile(r"何歳まで"),
}

_PUNCT_RE = re.compile(r"[\s　、。，．,.!！?？・「」『』（）()\[\]【】〜~\-ー:：;；/／]+")


# --- 動的事実 (価格・在庫・セール等) の禁止語。generate_faq_seo と共有 (SSOT はここ) ---
DYNAMIC_FACT_WORDS: tuple[str, ...] = (
    "価格", "最安", "在庫", "セール", "割引", "%オフ",
    "送料", "キャンペーン", "タイムセール",
)
# 「円」「ポイント」は素の語で弾くと知育玩具の記事で誤検出が出る (実測: 既存 FAQ
# 4,627 件中 12 件)。「楕円形・円盤・円が4個」(図形パズル/形合わせ商品の説明) や
# 「着目ポイント」(= 要点の意味) は動的事実ではないので残す。禁止したいのは
# 「2,955円」「数百円」のような金額と、Amazon のポイント還元のみ。
_PRICE_NUMBER_RE = re.compile(r"[\d０-９一二三四五六七八九十百千万数][\d,，]*\s*円")
_POINT_REWARD_RE = re.compile(
    r"ポイント(還元|付与|進呈|アップ|バック|倍)|ポイントが?(貯ま|付く|つく)|ポイ活"
)


def contains_dynamic_fact(text: str) -> bool:
    """時間で変化する事実 (価格・在庫・セール等) の禁止語を含むかを判定する。

    「円」「ポイント」は金額・ポイント還元の文脈でのみ弾く (素の語で弾くと
    「楕円形」「着目ポイント」のような静的な記述まで落ちるため)。
    """
    if any(word in text for word in DYNAMIC_FACT_WORDS):
        return True
    if _PRICE_NUMBER_RE.search(text):
        return True
    return bool(_POINT_REWARD_RE.search(text))


# --- 読者の邪魔になる FAQ を外す (K の決定 2026-11-18) ---
# 凍った価格・最安の Q&A は、冒頭の箱と価格カードが最新の値で答えているので出さない。
# ただしメーカー希望小売価格・定価・発売日の Q&A (変わらない値) は残す。
_FIXED_PRICE_RE = re.compile(r"希望小売価格|メーカー希望|定価|発売日")
_STRONG_DYNAMIC_RE = re.compile(r"最安|在庫|セール|割引|%オフ|ポイント|送料|キャンペーン|執筆時点|現在")
_WHERE_TO_BUY_RE = re.compile(r"どこで(買|購入|売)|販売店|どこに売")

# 対象年齢を言い直すだけの Q&A: 答えを文に分け、年齢の文と定型の注意文を除いて何も
# 残らなければ外す。年齢は冒頭の「対象年齢：」と題に出ている。
_AGE_Q_RE = re.compile(r"何歳から|何歳ですか|何歳でも|何ヶ月から|対象年齢|いつから(遊|使)")
_AGE_SENT_RE = re.compile(
    r"(対象年齢|推奨年齢|公式).*\d+\s*(歳|才|ヶ月|か月|カ月|ケ月)(以上|から|〜|~)?"
    r"|\d+\s*(歳|才|ヶ月|か月|カ月|ケ月)(以上|から)(の|を|が)?.*(対象|推奨|遊べ|使え)"
)
_AGE_BOILER_RE = re.compile(
    r"小さな部品|小さい部品|小さなパーツ|パーツ|誤飲|口に入れ|手の届かない|年齢を守|見守り|"
    r"安全のため|注意|推奨されます|設定されています|ご使用ください|ご利用ください"
)


# 年齢の文でも、ここに当たれば年齢以外の中身 (目安・難しさ・口コミなど) を言っているので残す
_AGE_EXTRA_RE = re.compile(
    r"ごろ|頃|くらい|ぐらい|ですが|しかし|難し|ルール|楽しめ|口コミ|レビュー|実際|握りやすい|誤飲しにく|"
    r"設計|理解|操作|表記|赤ちゃん|ふれあい|上手|道具|動き|指先|大人の|コレクション|"
    r"ませんが|ますが|部品がなく|安全な|出産祝い|0歳|下限|一般的|想定|新生児|ファースト|最適|大人向け|ホビー|始める"
)


def is_frozen_price_faq(question: str, answer: str) -> bool:
    """凍った価格・最安などの動的事実を言う Q&A か (定価・希望小売価格・発売日の Q&A は除く)。"""
    if not (contains_dynamic_fact(question) or contains_dynamic_fact(answer)):
        return False
    if _FIXED_PRICE_RE.search(answer) and not _STRONG_DYNAMIC_RE.search(answer + question):
        return False
    return True


def is_age_restatement_faq(question: str, answer: str) -> bool:
    """対象年齢を言い直すだけ (年齢の文と定型の注意文しか答えに無い) の Q&A か。"""
    if not _AGE_Q_RE.search(unicodedata.normalize("NFKC", question)):
        return False
    text = unicodedata.normalize("NFKC", answer)
    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", text) if s.strip()]
    if not sentences:
        return False
    for s in sentences:
        if _AGE_EXTRA_RE.search(s):
            return False
        if _AGE_SENT_RE.search(s) or _AGE_BOILER_RE.search(s):
            continue
        return False
    return True


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", text).lower())


def _name_tokens(product_names: list[str]) -> list[str]:
    tokens: set[str] = set()
    for name in product_names:
        if not isinstance(name, str):
            continue
        for tok in re.split(r"[\s　]+", unicodedata.normalize("NFKC", name).lower()):
            tok = _PUNCT_RE.sub("", tok)
            if len(tok) >= 2:
                tokens.add(tok)
    # 長い語から消す (部分文字列の取り残しを避ける)
    return sorted(tokens, key=len, reverse=True)


def _residual(question: str, tokens: list[str]) -> str:
    q = _norm(question)
    for tok in tokens:
        q = q.replace(tok, "")
    return q


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _intents(question: str) -> set[str]:
    q = unicodedata.normalize("NFKC", question)
    return {
        name
        for name, pat in _INTENT_PATTERNS
        if pat.search(q) and not (name in _INTENT_EXCLUDE and _INTENT_EXCLUDE[name].search(q))
    }


def _similar(res_a: str, res_b: str) -> bool:
    if len(res_a) >= 8 and len(res_b) >= 8 and difflib.SequenceMatcher(
        None, res_a, res_b, autojunk=False
    ).ratio() >= SEQUENCE_THRESHOLD:
        return True
    a, b = _bigrams(res_a), _bigrams(res_b)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SIMILARITY_THRESHOLD


def _clean_items(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        q, a = it.get("question"), it.get("answer")
        if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
            out.append(it)
    return out


def merge_faq(
    faq: Any,
    faq_extended: Any,
    product_names: list[str] | None = None,
    *,
    max_items: int = MAX_MERGED_FAQ_ITEMS,
    age_shown: bool = False,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """``faq_extended`` を先に、``faq`` の重複しないものを後ろに並べて ``max_items`` で切る。

    ``stats`` を渡すと ``dup_dropped`` (重複で落とした数) と ``cap_dropped`` (上限で落とした数)、
    ``dynamic_dropped`` (凍った価格などで落とした数。ext/faq 両方)、``age_dropped`` (年齢の言い直しで落とした数。``age_shown`` が真のときだけ)、``where_kept`` (「どこで買える」型で残した数)、``added`` (faq_extended のある記事で ``faq`` から足した数) を加算する。入力は変更しない。
    """
    dyn = age_dropped = where_kept = 0

    def _reader_filter(items: list[dict[str, str]]) -> list[dict[str, str]]:
        nonlocal dyn, age_dropped, where_kept
        kept: list[dict[str, str]] = []
        for it in items:
            if is_frozen_price_faq(it["question"], it["answer"]):
                dyn += 1
            elif age_shown and is_age_restatement_faq(it["question"], it["answer"]):
                age_dropped += 1
            else:
                if _WHERE_TO_BUY_RE.search(unicodedata.normalize("NFKC", it["question"])):
                    where_kept += 1
                kept.append(it)
        return kept

    ext_items = _reader_filter(_clean_items(faq_extended))
    art_items = _reader_filter(_clean_items(faq))
    # 商品名の語: product.name に加え、同じページの複数の質問に共通して出る語
    # (記事側は長い商品名、サイドカー側は短い商品名で書くことが多いため)。
    names = list(product_names or [])
    counts: dict[str, int] = {}
    for it in ext_items + art_items:
        # 最後の語には質問の本体 (助詞以降) が付いているので数えない
        words = re.split(r"[\s　]+", it["question"].strip())[:-1]
        for tok in set(_name_tokens(words)):
            counts[tok] = counts.get(tok, 0) + 1
    names.extend(tok for tok, n in counts.items() if n >= 2)
    tokens = _name_tokens(names)
    merged: list[dict[str, Any]] = []
    keys: list[tuple[str, set[str]]] = []  # (residual, intents)
    dup = cap = added = 0

    ext_merged: list[dict[str, Any]] = []
    for src_is_article, items in ((False, ext_items), (True, art_items)):
        if src_is_article:
            ext_merged = list(merged)
        for it in items:
            question = it["question"]
            res = _residual(question, tokens)
            norm_full = _norm(question)
            intents = _intents(question)
            is_dup = False
            # faq_extended は今までそのまま出ていたので、その中身は落とさない。
            # 照合先は faq_extended だけ (記事側どうしは同じ書き手が一緒に書いたもので、
            # これまで faq_extended の無い記事ではそのまま出ていたので落とさない)。
            for (r, ints), m in zip(keys, ext_merged) if src_is_article else ():  # keys は faq_extended の分だけ
                if (
                    norm_full == _norm(m["question"])
                    or (res and res == r)
                    or _similar(res, r)
                    or (intents & ints)
                ):
                    is_dup = True
                    break
            if is_dup:
                dup += 1
                continue
            # 上限は faq_extended のある記事の記事側 Q&A にだけ掛ける。faq_extended の
            # 無い記事の faq は従来どおり全部出す (faq_extended 自体も落とさない)。
            if src_is_article and ext_items and len(merged) >= max_items:
                cap += 1
                continue
            merged.append(it)
            if not src_is_article:
                keys.append((res, intents))
            elif ext_items:
                added += 1

    if stats is not None:
        stats["dup_dropped"] = stats.get("dup_dropped", 0) + dup
        stats["cap_dropped"] = stats.get("cap_dropped", 0) + cap
        stats["added"] = stats.get("added", 0) + added
        stats["dynamic_dropped"] = stats.get("dynamic_dropped", 0) + dyn
        stats["age_dropped"] = stats.get("age_dropped", 0) + age_dropped
        stats["where_kept"] = stats.get("where_kept", 0) + where_kept
    return merged
