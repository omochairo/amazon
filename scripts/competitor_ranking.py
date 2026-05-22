"""類似商品 (competitive_analysis) のランキング・候補プール構築ユーティリティ。

Plan D+ 実装の純粋関数群。fetch_amazon.py から呼ばれ、ユニットテストは
PA-API をモックすれば全部 stdlib だけで通る。

設計:
- ``build_search_keyword(title)``       … target タイトルから検索キーワードを抽出
- ``title_similarity(a, b)``            … char 2-gram Jaccard
- ``price_proximity(target, cand)``     … 価格距離スコア (0-1)
- ``rank_candidates(...)``              … タイトル類似度 + 価格 + 内部リンク boost
- ``ensure_internal_link_floor(...)``   … 自社記事 ASIN を最低 1 件 top-N に残す
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ブランド括弧プレフィックス (アガツマ(AGATSUMA) など)、末尾の販促括弧、記号類を剥がす
_PAREN_BRACKET_RE = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")
_PUNCT_RE = re.compile(r"[!?！？「」『』、。・,.\-_/|｜]+")
_WHITESPACE_RE = re.compile(r"\s+")

# 知育玩具カテゴリ全記事に頻出する filler 語。検索キーワードからは外す
# (これらを残すと「知育玩具」だけが共通になって target の特徴が消える)
_FILLER_WORDS = {
    "知育玩具", "知育", "おもちゃ", "玩具", "プレゼント", "ギフト", "誕生日",
    "男の子", "女の子", "こども", "子供", "子ども", "キッズ", "幼児",
    "1歳", "2歳", "3歳", "4歳", "5歳", "6歳", "歳", "才",
    "セット", "おすすめ", "新作", "正規品", "公式", "限定",
    "for", "kids", "toys", "toy", "gift",
}


# 助詞切り離し処理で誤判定（副作用）を起こすひらがな固有名詞等の例外辞書
_EXCEPTION_WORDS = {
    "のりもの", "おままごと", "いつまで", "おかいもの", "なに"
}

_JOSHI_START = set("のでに")
_JOSHI_END = set("のでにとをはがやもへ")


def _normalize(text: str) -> str:
    """括弧で囲まれた装飾語と記号を剥がして単語抽出に適した形に。"""
    if not text:
        return ""
    s = _PAREN_BRACKET_RE.sub(" ", text)
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()

    # 1. 漢字・カタカナの直後にある助詞（の, に, と, を, は, が, や, も, で, へ）をスペースで切り離す
    s = re.sub(r"(?<=[一-龥々々ァ-ヶー])([のにとをはがやもでへ])", r" \1 ", s)

    # 2. ひらがなが2文字以上続いた直後にあり、かつ後ろがひらがなではない助詞をスペースで切り離す
    # ただし、例外辞書に含まれる単語は切り離さない
    def split_joshi_hiragana(match: re.Match) -> str:
        word = match.group(0)
        if word in _EXCEPTION_WORDS:
            return word
        return f"{match.group(1)} {match.group(2)} "

    s = re.sub(r"([ぁ-ん]{2,})([のにとをはがやもでへ])(?=[^ぁ-ん]|$)", split_joshi_hiragana, s)

    return _WHITESPACE_RE.sub(" ", s).strip()


def _is_filler(token: str) -> bool:
    """助詞ストリップを考慮して filler 語か判定する。"""
    low = token.lower()
    if low in _FILLER_WORDS:
        return True

    # 末尾の助詞を剥がして判定
    if len(token) >= 3 and token[-1] in _JOSHI_END:
        if token[:-1].lower() in _FILLER_WORDS:
            return True

    # 先頭の助詞を剥がして判定
    if len(token) >= 3 and token[0] in _JOSHI_START:
        if token[1:].lower() in _FILLER_WORDS:
            return True

    # 両方を剥がして判定
    if len(token) >= 4 and token[0] in _JOSHI_START and token[-1] in _JOSHI_END:
        if token[1:-1].lower() in _FILLER_WORDS:
            return True

    return False


def _tokens(text: str) -> list[str]:
    """カナ/漢字/ひらがな/英数字を適切に考慮してトークンを抽出。"""
    if not text:
        return []
    normalized = _normalize(text)

    # 漢字・カタカナ・ひらがなが交互に現れる複合語を一体として抽出
    # [ひらがな任意個] + [漢字/カタカナ連続 + ひらがな任意個] の1回以上の繰り返し
    # またはひらがなのみ（2文字以上）、または英数字
    raw = re.findall(r"[ぁ-ん]*(?:[ァ-ヶー一-龥々々]+[ぁ-ん]*)+|[ぁ-ん]{2,}|[A-Za-z0-9]+", normalized)
    out = []
    for t in raw:
        if len(t) < 2:
            continue
        if _is_filler(t):
            continue
        out.append(t)
    return out


def build_search_keyword(title: str, max_tokens: int = 3) -> str:
    """target タイトルから SearchItems 用キーワードを構築。

    ブランド括弧 + filler を剥がした後、先頭から ``max_tokens`` 個を空白連結。
    PA-API SearchItems は 1-3 語くらいが最も適切な結果を返す経験則。
    """
    toks = _tokens(title)
    if not toks:
        return title[:30] if title else ""
    return " ".join(toks[:max_tokens])


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    """char N-gram。形態素解析依存を避けるため stdlib のみで類似度判定する。"""
    s = _normalize(text).lower().replace(" ", "")
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on char 2-grams (0.0-1.0)。

    タイトルの主要要素 (商品名/ブランド/機能語) が重なれば高くなる。
    filler 抜きの token 列を ngram にすると 1-2 字の機能語まで効くので、
    まず ``_tokens`` で filler を落としてから join、ngram 化する。
    """
    sa = _char_ngrams(" ".join(_tokens(a)))
    sb = _char_ngrams(" ".join(_tokens(b)))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def price_proximity(target_price: int, cand_price: int) -> float:
    """価格が近いほど 1.0 に近づくスコア (0.0-1.0)。

    target_price=0 または cand_price=0 の時は 0.5 (中立)。
    Δ/target が 1.0 (= 倍 or 半額) で約 0.5、5.0 で約 0.17。
    """
    if target_price <= 0 or cand_price <= 0:
        return 0.5
    diff_ratio = abs(target_price - cand_price) / target_price
    return 1.0 / (1.0 + diff_ratio)


def score_candidate(
    target: dict[str, Any], cand: dict[str, Any], covered_asins: set[str],
    w_title: float = 0.6, w_price: float = 0.3, w_internal: float = 0.1,
) -> float:
    """単一候補の合成スコア。内部リンク候補には小さい boost を加える。"""
    sim = title_similarity(target.get("title") or "", cand.get("title") or cand.get("name") or "")
    pp = price_proximity(int(target.get("price") or 0), int(cand.get("price") or 0))
    internal = 1.0 if cand.get("asin") in covered_asins else 0.0
    return w_title * sim + w_price * pp + w_internal * internal


def dedupe_candidates(pools: Iterable[Iterable[dict]], exclude_asin: str) -> list[dict]:
    """複数プールを ASIN 単位で重複排除、target を除外。
    先に出てきた dict を優先 (= 後発の SearchItems 結果は先に渡す想定)。
    """
    seen: dict[str, dict] = {}
    for pool in pools:
        for item in pool:
            asin = item.get("asin")
            if not asin or asin == exclude_asin:
                continue
            if asin in seen:
                continue
            if not item.get("image"):
                continue
            seen[asin] = item
    return list(seen.values())


def rank_candidates(
    target: dict[str, Any], candidates: list[dict], covered_asins: set[str],
) -> list[dict]:
    """合成スコア降順で並べる。元の dict を破壊しないようにコピー。"""
    scored = [(score_candidate(target, c, covered_asins), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _s, c in scored]


def ensure_internal_link_floor(
    ranked: list[dict], covered_asins: set[str], total: int = 5,
    min_internal: int = 1, min_score_for_floor: float = 0.15,
    target: dict[str, Any] | None = None,
) -> list[dict]:
    """top-N に内部リンク候補を最低 ``min_internal`` 件残す floor 適用。

    既にランキング上位が internal なら何もしない。internal が 0 件なら
    ranked の中から最上位の internal 候補を末尾 slot に差し込む。ただし
    関連性が極端に低い (`title_similarity < min_score_for_floor`) と
    「ぬいぐるみ記事にブロック記事をねじ込む」ような事故になるので
    閾値ガードで弾く。
    """
    if total <= 0:
        return []
    top = ranked[:total]
    internal_in_top = sum(1 for c in top if c.get("asin") in covered_asins)
    if internal_in_top >= min_internal:
        return top
    target_title = (target or {}).get("title") or ""
    for c in ranked[total:]:
        if c.get("asin") not in covered_asins:
            continue
        cand_title = c.get("title") or c.get("name") or ""
        if title_similarity(target_title, cand_title) < min_score_for_floor:
            continue
        # 末尾の non-internal を 1 件 demote して internal を入れる
        replaced = False
        for i in range(len(top) - 1, -1, -1):
            if top[i].get("asin") not in covered_asins:
                top[i] = c
                replaced = True
                break
        if replaced:
            internal_in_top += 1
            if internal_in_top >= min_internal:
                break
    return top
