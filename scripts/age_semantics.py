"""対象年齢の読み取りで、数字以外の手がかりを扱う共有ルール (#9186)。

build_post.py (front matter の age_min_months) と build_feature_lists.py
(特集リスト・価格ダッシュボード・年齢別 hub) の両方から使う。2 つの読み取り
処理で結果が食い違わないよう、規則はここにだけ書く。

- min_months_from_words: 数字の無い年齢表記 (「小学生以上」「大人向け」) を月齢にする
- is_seasonal_decoration: 節句・正月の飾り物か。「0歳〜」は遊ぶ年齢ではなく初節句を
  迎える赤ちゃんを指すので、対象年齢を持たない (年齢で絞る機能に出さない) 扱いにする
"""
from __future__ import annotations

import re
from typing import Any

# 18 歳。これ以上は大人向け (年齢別 hub・診断の対象外)。
ADULT_AGE_MONTHS = 216

# 数字の無い表記だけに使う (数字があればそちらを優先する)。複数当たれば最小を取る
# (「幼児〜小学生」は 36)。「子供から大人まで」は大人向けとは読まない。
_AGE_WORDS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"大人向け|大人用|成人向け"), ADULT_AGE_MONTHS),
    (re.compile(r"高校生(?!未満)"), 180),
    (re.compile(r"中学生(?!未満)"), 144),
    (re.compile(r"小学生(?!未満)"), 72),
    (re.compile(r"幼児(?!未満)"), 36),
)

# 商品名で判定する。Amazon のカテゴリ (兜飾り・こいのぼり・つるし雛 等) は、
# 「正月飾り」に福笑い、「季節用品」に水鉄砲のような遊び道具も入っているので使わない。
# 「ひな祭り」の工作キットや福笑い・かるた・こまは遊び道具なので当てない。
_SEASONAL_DECORATION_RE = re.compile(
    r"五月人形|兜飾り|鎧飾り|雛飾り|雛人形|ひな人形|つるし雛|つるし飾り"
    r"|こいのぼり|鯉のぼり|鯉飾り|鏡餅|陣羽織|破魔弓|弓太刀|節句飾り"
    r"|飾り.{0,3}羽子板|羽子板飾り|しめ飾り|門松"
)


def min_months_from_words(text: Any) -> int | None:
    """数字の無い年齢表記から最小月齢を読む。読めなければ None。"""
    if not text:
        return None
    s = str(text)
    found = [months for rx, months in _AGE_WORDS if rx.search(s)]
    return min(found) if found else None


def is_seasonal_decoration(*names: Any) -> bool:
    """商品名 (name / name_full 等) のどれかが節句・正月の飾り物を指しているか。"""
    return any(n and _SEASONAL_DECORATION_RE.search(str(n)) for n in names)


def seasonal_names(raw: dict[str, Any]) -> tuple[Any, ...]:
    """記事 json から、is_seasonal_decoration に渡す商品名を集める。"""
    product = raw.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    return (product.get("name"), product.get("name_full"), raw.get("product_name"))
