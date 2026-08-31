"""#5083 項目2: seo_title の組み立て。

押さえるのは「短くなるか」ではなく **記事が持っていないことを名乗らないか**
(/deals/ が価格据え置きに「今セール中」と書いていた #6285 と同じクラス) と、
**どのページも現行より長いタイトルにならないか** の 2 点。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seo_title  # noqa: E402


def _article(*, name="テスト積み木", review=None, target_age=None):
    product = {"name": name}
    if target_age is not None:
        product["target_age"] = target_age
    article = {"product": product}
    if review is not None:
        article["review_signals"] = review
    return article


# --- テールの選択 (名乗ってよいことだけ名乗る) ---------------------------------

def test_口コミも対象年齢もあるとき両方名乗る():
    a = _article(review=["よかった"], target_age="3歳〜")
    assert seo_title.choose_tail(a) == seo_title.TAIL_REVIEW_AGE


def test_口コミが無いとき口コミと名乗らない():
    a = _article(review=[], target_age="3歳〜")
    tail = seo_title.choose_tail(a)
    assert "口コミ" not in tail
    assert tail == seo_title.TAIL_AGE


def test_review_signals_キー自体が無くても口コミと名乗らない():
    a = _article(target_age="3歳〜")
    assert "口コミ" not in seo_title.choose_tail(a)


def test_対象年齢が空文字なら年齢を名乗らない():
    a = _article(review=["よかった"], target_age="   ")
    assert seo_title.choose_tail(a) == seo_title.TAIL_REVIEW


def test_材料が何も無ければ最安値だけ():
    assert seo_title.choose_tail(_article()) == seo_title.TAIL_PRICE


def test_どこで買える型は専用テール():
    a = _article(review=["よかった"], target_age="3歳〜")
    assert seo_title.choose_tail(a, where_to_buy=True) == seo_title.TAIL_WHERE_TO_BUY


# --- 組み立て -----------------------------------------------------------------

def test_商品名を先頭に置いてテールを足す():
    a = _article(name="ロンビー", review=["よかった"], target_age="4歳〜")
    got = seo_title.build_seo_title(a, "ロンビー(Lon-Bi)は知育に効く？類似ブロックとの違いを検証")
    assert got == "ロンビー" + seo_title.TAIL_REVIEW_AGE


def test_現行より短くならないなら出さない():
    a = _article(name="ロンビー", review=["よかった"], target_age="4歳〜")
    # 現行 title が既に短い場合、伸ばしてまで置き換えない
    assert seo_title.build_seo_title(a, "ロンビー") is None


def test_同じ長さでも出さない():
    a = _article(name="あ", review=None, target_age=None)  # "あ" + "の最安値" = 5
    assert seo_title.build_seo_title(a, "12345") is None


def test_商品名が取れなければ出さない():
    assert seo_title.build_seo_title({"product": {}}, "なにかの長いタイトル") is None
    assert seo_title.build_seo_title({}, "なにかの長いタイトル") is None


def test_どこで買える型の組み立て():
    a = _article(name="ロンビー", review=["よかった"], target_age="4歳〜")
    current = "ロンビーはどこで買える？在庫と価格を毎日チェック（Amazon/楽天）"
    got = seo_title.build_seo_title(a, current, where_to_buy=True)
    assert got == "ロンビー" + seo_title.TAIL_WHERE_TO_BUY
    assert len(got) < len(current)
