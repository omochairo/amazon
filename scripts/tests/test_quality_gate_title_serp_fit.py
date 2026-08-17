"""#5083 項目2: title の先頭 30 字に「商品名 + 検索意図語 1 つ」が収まるか。

前提となる実測 (data/articles 2,061 本、2026-08-17):
- 先頭 30 字に product.name が入る 85.0% / 意図語が入る 53.3% / 両方 53.3%。
  束縛条件は意図語だけで、識別性はほぼ無料。
- 直近 (2026-07-16 以降) の 571 本では両方が 45.0% と corpus 全体より低い。
  放っておいて改善する類ではない。
- 落ちている記事の原因は 3 つに分かれ、直す場所が違う:
    A. product.name 自体が 26 字超 (意図語を置く余地が無い) … 20.5%
    B. product.name は短いのに title 側で語を盛り直している … 11.6%
    C. 名前も長さも問題ないが意図語が後ろ                  … 67.9%
  メッセージがこの 3 つを撃ち分けることを固定する。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import (  # noqa: E402
    TITLE_SERP_FIT_SOFT_SCORE,
    TITLE_SERP_HEAD_CHARS,
    TITLE_SERP_MAX_NAME_CHARS,
    check_title_serp_fit,
)


def _article(title: str) -> dict:
    return {"slug": "2026-08-17-B0XXXXXXXX", "title": title}


def test_name_then_intent_within_head_passes():
    name = "LumiLumi タイマー 子供"
    r = check_title_serp_fit(_article(f"{name}の最安値を3サイト横断比較"), name)
    assert r.passed and r.score == 1.0
    assert "最安値" in r.message


def test_intent_just_past_the_head_fires():
    """先頭 30 字の 1 文字外に落ちた意図語は「見えていない」。"""
    name = "JoyGrow 形合わせ マッチングエッグ"
    title = f"{name}の知育効果は？特徴・遊び方を解説"
    assert title.find("遊び方") >= TITLE_SERP_HEAD_CHARS  # 前提を固定する
    r = check_title_serp_fit(_article(title), name)
    assert r.passed, "warn-only なので合否は変えない"
    assert r.score == TITLE_SERP_FIT_SOFT_SCORE
    assert "検索意図語が先頭" in r.message


def test_long_product_name_points_at_the_name(recwarn=None):
    """原因A: 名前が長すぎる。直す場所は title ではなく product.name。"""
    name = "カワダ このピース絶対はまらなそうで完璧にハマるパズル トライアングル"
    assert len(name) > TITLE_SERP_MAX_NAME_CHARS
    r = check_title_serp_fit(_article(f"{name} 最安値・対象年齢を3サイト横断で徹底比較"), name)
    assert r.score == TITLE_SERP_FIT_SOFT_SCORE
    assert "短い通称" in r.message
    assert str(len(name)) in r.message


def test_padded_title_points_at_the_title():
    """原因B: name は短いのに title が型番・英字併記を盛り直している。"""
    name = "くもんの日本地図パズル"
    title = ("くもん出版(KUMON PUBLISHING) くもんの日本地図パズル "
             "日本の世界遺産すごろく付きの最安値・対象年齢を徹底比較")
    r = check_title_serp_fit(_article(title), name)
    assert r.score == TITLE_SERP_FIT_SOFT_SCORE
    assert "足し直さない" in r.message
    # 名前が短いことは分かっているので、長さのせいにしない。
    assert "短い通称" not in r.message


def test_one_intent_word_is_enough():
    """意図語を並べる必要は無い。1 つ前半にあれば満点。"""
    name = "HMshuo バランスゲーム"
    r = check_title_serp_fit(_article(f"{name}の口コミ"), name)
    assert r.score == 1.0


def test_colloquial_age_query_counts_as_intent():
    """「何歳から」は「対象年齢」の口語形なので同じ意図として数える。"""
    name = "Bacolos 木製知育パズル"
    r = check_title_serp_fit(_article(f"{name}は何歳から遊べる？特徴を徹底解説"), name)
    assert r.score == 1.0


def test_intent_word_outside_head_reports_its_position():
    name = "テンヨー ジグソーパズル"
    title = f"{name} 500ピース ステンドアート仕様のくわしい解説と最安値"
    r = check_title_serp_fit(_article(title), name)
    assert f"{title.find('最安値')} 字目" in r.message


def test_missing_intent_word_entirely_is_reported():
    name = "森のうんどう会 マルチカラー"
    r = check_title_serp_fit(_article(f"{name}でひろがる室内あそびの世界"), name)
    assert "タイトル全体に無し" in r.message


def test_empty_title_is_left_to_check_title_seo():
    """空 title は hard fail 側 (check_title_seo) の担当。二重に鳴らさない。"""
    r = check_title_serp_fit(_article(""), "なにか")
    assert r.passed and r.score == 1.0
    assert "skipped" in r.message


def test_never_fails_the_article():
    """warn-only。記事生成レーンは auto-merge なので合否は絶対に変えない。"""
    for title, name in (
        ("", "x"),
        ("まったく関係ないタイトル", "商品名"),
        ("商品名の最安値", "商品名"),
    ):
        assert check_title_serp_fit(_article(title), name).passed


def test_head_window_is_the_serp_cutoff():
    """しきい値は SERP の打ち切り位置 (全角 30 字) に置く。"""
    assert TITLE_SERP_HEAD_CHARS == 30
    # 最短の意図語表現「の最安値」を置ける余地を名前の上限にする。
    assert TITLE_SERP_MAX_NAME_CHARS == TITLE_SERP_HEAD_CHARS - len("の最安値")
