"""#5083: 商品ページの ``<title>`` を SERP の表示窓に合わせて詰める。

## なぜ front matter の ``title`` を書き換えないのか

``title`` は ``<h1>``・一覧カード・内部リンクのアンカーにも使われる。読者向けの
文脈は長いほうがよく、短くしたいのは **SERP に出る ``<title>`` だけ**。そこで
``seo_title`` を別に持たせ、``head.html`` の ``<title>`` だけがそちらを見る。
撤回するときは front matter を出すのをやめれば元に戻る。

## 予算 (2026-08-31 実測)

日本語 SERP のタイトルは全角 30〜35 文字程度で切れる。配信中の全商品ページを
数えると:

===================================  ======  ====  ====
                                     中央値   p90   最大
===================================  ======  ====  ====
商品名                                   21    31    50
テール (「の口コミ・最安値・…」等)         25    32    47
サイト名サフィックス (「 | 比較ナビ」)      7     7     7
===================================  ======  ====  ====

**サイト名の短縮 (31 → 7 文字) は #5083 項目1 で済んでいる。** 残っているのは
テールで、中央値 25 文字は窓の外に落ちる。

ただし **テールを詰めても全ページは 32 文字に入らない。** 商品名だけで 25 文字を
超えるページが 419/1,525 (27.5%)、商品名単体で 32 文字を超えるページが 109 枚
(7.1%) ある。後者はテールをゼロにしても切れるのは名前の中なので、この修正では
救えない。**商品名の正規化は別の課題**として残す (#5083 に記録)。

シミュレーションでは 3,154 記事の合計長が 中央値 48 → 36 文字、32 文字以内に
収まるページが 835 → 1,165 枚になる。

## テールが主張してよいこと

「口コミ」を持たない記事のタイトルに「口コミ」と書かない。/deals/ が価格据え置きの
商品に「今セール中」と書いていた #6285 と同じクラスの問題になる。したがって
テールは記事データの実体から決める。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 「どこで買える/在庫」記事型 (#2686 / #4964) のテール。決定的タイトル
# 「…はどこで買える？在庫と価格を毎日チェック（Amazon/楽天/…）」は 44 文字あり、
# 窓に入るのは先頭の「はどこで買える？」までなので、そこだけ残す。
TAIL_WHERE_TO_BUY = "はどこで買える？"

TAIL_REVIEW_AGE = "の口コミ・最安値・対象年齢"
TAIL_REVIEW = "の口コミ・最安値"
TAIL_AGE = "の最安値・対象年齢"
TAIL_PRICE = "の最安値"


def _has_review_signals(article: Dict[str, Any]) -> bool:
    """記事が実際に口コミの材料を持っているか。

    持っていないのに「口コミ」と名乗らせないための判定。空リスト / 空 dict は
    「無い」として扱う。
    """
    signals = article.get("review_signals")
    if isinstance(signals, (list, tuple, dict, str)):
        return len(signals) > 0
    return bool(signals)


def _has_target_age(article: Dict[str, Any]) -> bool:
    product = article.get("product")
    if not isinstance(product, dict):
        return False
    value = product.get("target_age")
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def choose_tail(article: Dict[str, Any], *, where_to_buy: bool = False) -> str:
    """記事データが裏づけられる範囲でいちばん情報量の多いテールを返す。"""
    if where_to_buy:
        return TAIL_WHERE_TO_BUY
    review = _has_review_signals(article)
    age = _has_target_age(article)
    if review and age:
        return TAIL_REVIEW_AGE
    if review:
        return TAIL_REVIEW
    if age:
        return TAIL_AGE
    return TAIL_PRICE


def build_seo_title(
    article: Dict[str, Any],
    current_title: str,
    *,
    where_to_buy: bool = False,
) -> Optional[str]:
    """``seo_title`` を組み立てる。出す価値が無いときは ``None`` を返す。

    ``None`` を返すのは次のいずれか:

    - 商品名が取れない (この関数は商品名を先頭に置く前提で組む)
    - 組み上がりが現行 ``title`` より短くならない —— **どのページも今より長い
      タイトルにならないことを保証する。** 短縮が目的なので、伸びるなら出さない
    """
    product = article.get("product")
    name = ""
    if isinstance(product, dict):
        name = (product.get("name") or "").strip()
    if not name:
        return None
    candidate = name + choose_tail(article, where_to_buy=where_to_buy)
    if len(candidate) >= len(current_title or ""):
        return None
    return candidate
