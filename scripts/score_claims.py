"""score_claims.py — 表示する点数と本文の言い切りの食い違いを見つける (navi-brain#165 T1)。

記事ページに出る点数は build 時に ``score_calculator`` が計算する (build_post.py)。
本文 (narrative) は記事生成 AI が書くので、点数と逆向きの言い切りが入りうる
(「安全性の高さも魅力」なのに安全性 2.0、「自信を持って推せる一品です」なのに 59 点)。
このモジュールはその食い違いを数え、数え方を 1 か所に置く。

数える食い違い (どちらも build と同じ計算の点で判定する):

(a) safety_claim — 安全性の軸が ``SAFETY_CLAIM_MAX_AXIS`` (2.6) 以下なのに、本文が
    安全性を強く言い切っている。2.6 は「tier D で認証も無い」(2.0) から
    「tier D + ST/CE 等の認証」(2.6) までで、ページの安全性バーが最低帯に見える範囲。
(b) recommend_claim — 総合が ``LOW_TOTAL_MAX`` (60) 以下なのに、本文が強い推奨の
    定型句を使っている。

言い切りの語は誤検出を抑えるため、**それだけで読者が「安全だ」「強く推している」と
受け取る強い表現**に絞る (SAFETY_CLAIM_PATTERNS / RECOMMEND_CLAIM_PATTERNS)。
数えないもの:
- 「安心して遊べる」「安全に配慮」「安全設計」「安全性を兼ね備え」— 角の丸み・素材など
  個別の設計を受けて書かれることが多く、点数と矛盾するとは限らない
- 「おすすめです」「候補に入れたい」「迷わず候補に」— 推奨の強さが弱い
- 「最高の」「間違いなし」単独 — 推奨以外 (「最高の状態で」「喜ぶこと間違いなし」) に多い
- 打ち消し・条件付き (「安全性が高いとは言えない」「安全性が高いかどうか」)

build 時の自動の置き換え (replace_low_score_closing) は、総合が 60 以下の記事の
closing にある**完全一致の定型句**だけを中立の固定文にする。安全性の言い切りは
文脈で意味が変わるので自動では書き換えない (数えるだけ)。
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from brand_normalizer import normalize as normalize_brand
from score_calculator import ScoreResult, calculate, compute_ivs_axes

# score_calculator が data/raw を読む既定の場所 (calculate の repo_root 既定と同じ)
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SAFETY_CLAIM_MAX_AXIS = 2.6
LOW_TOTAL_MAX = 60

# (a) 安全性の強い言い切り。(ラベル, 正規表現)。ラベルは集計のキー。
SAFETY_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("安全性が高い", re.compile(
        r"安全性(?:が|は|も|の)?(?:非常に|とても|特に|極めて|かなり)?高[いくさかめ]")),
    ("高い安全性", re.compile(r"(?:非常に|とても|極めて)?高い安全性")),
    ("安全性が抜群", re.compile(
        r"安全性(?:が|は|も|の)?(?:抜群|申し分|折り紙付き|お墨付き|優れ|優秀)")),
    ("安全面で優れる", re.compile(r"安全面(?:で|も|は|に)?(?:も)?(?:優れ|申し分|抜群|安心)")),
    ("安心・安全", re.compile(r"安心[・･]?安全|安全[・･]?安心")),
    ("口に入れても安心", re.compile(
        r"(?:口に入れ|舐め|なめ|かじ)ても(?:安心|安全|大丈夫|問題(?:ない|なく|ありません))")),
)

# (b) 強い推奨の定型句。
_REC_VERB = r"(?:推|おすすめ|お勧め|オススメ|勧め|すすめ|選べ|選ん|選び)"
RECOMMEND_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("自信を持って推せる", re.compile(rf"自信を(?:持|も)って(?:強く)?(?:{_REC_VERB}|太鼓判)")),
    ("迷わず推せる", re.compile(rf"迷わず(?:{_REC_VERB}|買)")),
    ("太鼓判", re.compile(r"太鼓判を押")),
    ("間違いなく推せる", re.compile(rf"間違いなく(?:{_REC_VERB}|買って損)")),
    ("文句なし", re.compile(rf"文句なし(?:に|の)?(?:{_REC_VERB}|一品|逸品)")),
)

# 言い切りの直後にこれが来たら、打ち消し・条件付き・問いかけなので数えない
# (「安全性が高いとは言えない」「口に入れても安全か」「舐めても大丈夫な素材か？」)。
_NEGATION_AFTER = re.compile(
    r"^.{0,4}(?:とは(?:言|い)え|とは限ら|かどうか|わけでは|ではな|ではありません|とまでは)"
    r"|^.{0,6}?か(?:[」』？?、。]|どうか|と(?:心配|不安|悩|気))")

# 安全性の語のあとにこれが来たら、商品の主張ではなく読者の選び方の話
# (「安全性の高さを重視するご家庭」「安全性が高いものを選ぶ」) なので数えない。
_SAFETY_GENERIC_AFTER = re.compile(
    r"^.{0,6}(?:重視|求め|優先|選び|選ぶ|選ん|大切|重要|気にする|気になる|チェック|確かめ|確認)")

# closing の完全一致の定型句 → 総合が低い記事で使う中立の固定文。
# 集計 (2026-11-13・ASIN ごとに最新の記事・総合 60 以下) で closing に多かったもの
# だけを載せる。前の語句 (「…の方には、」「…として」) にそのままつながる形にしてある。
# 置き換え後の文は RECOMMEND_CLAIM_PATTERNS に当たらない (テストで固定)。
CLOSING_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("自信を持って推せる一品です。", "検討する価値のある一品です。"),
    ("自信を持って強く推せる一品です。", "検討する価値のある一品です。"),
    ("自信を持っておすすめできる一品です。", "検討する価値のある一品です。"),
    ("自信を持って推せるアイテムです。", "検討する価値のあるアイテムです。"),
    ("自信を持っておすすめできます。", "候補の一つとして検討できます。"),
    ("迷わず推せる一品です。", "検討する価値のある一品です。"),
)

NARRATIVE_SECTIONS = (
    "lead", "why_this_product", "gift_appeal", "daily_use",
    "safety_note", "closing", "how_to_choose",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")


@dataclass(frozen=True)
class ScoreView:
    """ページに出る点 (総合と 4 軸)。"""
    total_100: int
    axes: dict

    @property
    def safety(self) -> float:
        return float(self.axes.get("safety", 5.0))


@dataclass(frozen=True)
class ClaimHit:
    kind: str       # "safety_claim" | "recommend_claim"
    label: str      # SAFETY_CLAIM_PATTERNS / RECOMMEND_CLAIM_PATTERNS のラベル
    section: str    # narrative のキー
    phrase: str     # 当たった語句
    sentence: str   # 当たった文


def view_from_result(sr: ScoreResult) -> ScoreView:
    return ScoreView(total_100=int(sr.total_100), axes=compute_ivs_axes(sr.breakdown))


def compute_view(article: dict, repo_root: Optional[pathlib.Path] = None) -> Optional[ScoreView]:
    """build と同じ計算で点を出す。ブランドが無い記事は build も点を出さないので None。

    build はこのあと価格の日次観測で product の価格を上書きしてから計算するので、
    生の記事 JSON から出す総合点はコスパの軸の分だけページとずれうる
    (安全性の軸はブランドと certifications だけで決まるのでずれない)。
    """
    product = article.get("product")
    if not isinstance(product, dict):
        return None
    raw_brand = product.get("brand")
    if not raw_brand:
        return None
    sr = calculate(article, normalize_brand(raw_brand), asin=product.get("asin"),
                   repo_root=repo_root)
    return view_from_result(sr)


def iter_sections(article: dict) -> Iterator[tuple[str, str]]:
    """narrative の (キー, 段落) を返す。値は文字列か文字列の配列。"""
    narrative = article.get("narrative")
    if not isinstance(narrative, dict):
        return
    keys = list(NARRATIVE_SECTIONS) + sorted(k for k in narrative if k not in NARRATIVE_SECTIONS)
    for key in keys:
        value = narrative.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str) and item:
                yield key, item


def _sentences(text: str) -> Iterator[str]:
    for s in _SENTENCE_SPLIT.split(text):
        s = s.strip()
        if s:
            yield s


def _match(patterns, sentence: str, generic_after=None) -> Iterator[tuple[str, str]]:
    for label, rx in patterns:
        for m in rx.finditer(sentence):
            rest = sentence[m.end():]
            if _NEGATION_AFTER.match(rest):
                continue
            if generic_after is not None and generic_after.match(rest):
                continue
            yield label, m.group(0)


def find_claims(article: dict, view: Optional[ScoreView]) -> list[ClaimHit]:
    """点数と食い違う言い切りを返す。点が無い (view=None) 記事は空。"""
    if view is None:
        return []
    check_safety = view.safety <= SAFETY_CLAIM_MAX_AXIS
    check_rec = view.total_100 <= LOW_TOTAL_MAX
    if not (check_safety or check_rec):
        return []
    hits: list[ClaimHit] = []
    for section, text in iter_sections(article):
        for sentence in _sentences(text):
            if check_safety:
                for label, phrase in _match(SAFETY_CLAIM_PATTERNS, sentence,
                                             _SAFETY_GENERIC_AFTER):
                    hits.append(ClaimHit("safety_claim", label, section, phrase, sentence))
            if check_rec:
                for label, phrase in _match(RECOMMEND_CLAIM_PATTERNS, sentence):
                    hits.append(ClaimHit("recommend_claim", label, section, phrase, sentence))
    return hits


def replace_low_score_closing(article: dict, view: Optional[ScoreView]) -> int:
    """総合が LOW_TOTAL_MAX 以下なら、closing の定型句を中立の固定文にする。

    article を直接書き換え、置き換えた箇所の数を返す。置き換えるのは
    CLOSING_REPLACEMENTS の完全一致だけ (それ以外の言い切りは触らない)。
    """
    if view is None or view.total_100 > LOW_TOTAL_MAX:
        return 0
    narrative = article.get("narrative")
    if not isinstance(narrative, dict):
        return 0
    closing = narrative.get("closing")

    def _sub(text: str) -> tuple[str, int]:
        n = 0
        for old, new in CLOSING_REPLACEMENTS:
            c = text.count(old)
            if c:
                text = text.replace(old, new)
                n += c
        return text, n

    total = 0
    if isinstance(closing, str):
        narrative["closing"], total = _sub(closing)
    elif isinstance(closing, list):
        out = []
        for item in closing:
            if isinstance(item, str):
                item, n = _sub(item)
                total += n
            out.append(item)
        narrative["closing"] = out
    return total


def phrases_to_avoid(view: ScoreView) -> list[str]:
    """生成前のプロンプトに渡す「この点数では書かない表現」(build_jules_prompt 用)。"""
    out: list[str] = []
    if view.safety <= SAFETY_CLAIM_MAX_AXIS:
        out.append("安全性の言い切り (「安全性が高い」「高い安全性」「安全性は抜群」"
                   "「安全面で優れる」「安心・安全」「口に入れても安心」など)")
    if view.total_100 <= LOW_TOTAL_MAX:
        out.append("強い推奨の定型句 (「自信を持って推せる」「迷わず選べる」「太鼓判」"
                   "「間違いなくおすすめ」「文句なし」など)")
    return out
