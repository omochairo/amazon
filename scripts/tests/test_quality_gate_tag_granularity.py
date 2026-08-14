"""#5088 タグ粒度 check の検査。

実データ (data/articles 1,977 本) の実測で確定した前提:
- 「イト」は文字列処理のバグではなく、商品名 "アークライト ito(イト)レインボー"
  の一部を生成側がタグに書いたもの。だから断片検出は文字数ではなく
  「同一記事内の別タグの部分文字列 かつ どの記事とも共有していない」で拾う。
- 2 文字タグ 101 種のうち断片は 1 種だけ (レゴ・学研・恐竜・1歳 等が大半)。
  文字数ベースの規則は入れない。
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import (  # noqa: E402
    TAG_NOVEL_RATIO_WARN,
    check_tag_granularity,
    load_tag_corpus,
)


def _article(tags, name="ジスター 天才のはじまり", name_full=""):
    return {
        "slug": "2026-08-14-B0XXXXXXXX",
        "tags": tags,
        "product": {"name": name, "name_full": name_full},
    }


def _corpus(**counts):
    return collections.Counter(counts)


SHARED = _corpus(**{"知育玩具": 500, "プレゼント": 400, "ブロック": 200, "レゴ": 89})


def test_no_tags_is_ok():
    result = check_tag_granularity({"tags": []}, SHARED)
    assert result.passed and result.score == 1.0


def test_shared_tags_pass_clean():
    result = check_tag_granularity(_article(["知育玩具", "プレゼント", "ブロック"]), SHARED)
    assert result.passed and result.score == 1.0


def test_never_fails_even_when_everything_is_wrong():
    """warn-only: 合否は変えない (#5088 やること 1)。"""
    result = check_tag_granularity(
        _article(["ジスター 天才のはじまり", "アークライト", "イト"]), SHARED
    )
    assert result.passed is True
    assert result.score < 1.0


def test_product_name_tag_is_flagged():
    result = check_tag_granularity(
        _article(["ジスター 天才のはじまり", "知育玩具", "プレゼント"]), SHARED
    )
    assert "product-name" in result.message
    assert result.score < 1.0


def test_product_name_match_ignores_spacing_and_case():
    article = _article(["NISSAN GT-R  NISMO"], name="nissan gt-r nismo")
    assert "product-name" in check_tag_granularity(article, SHARED).message


def test_name_full_also_counts_as_product_name():
    article = _article(
        ["アークライト ito (イト) レインボー"],
        name="アークライト ito(イト)レインボー",
        name_full="アークライト ito (イト) レインボー (2-14人用)",
    )
    assert "product-name" in check_tag_granularity(article, SHARED).message


def test_fragment_inside_another_tag_is_flagged():
    """「イト」⊂「アークライト」。実データで唯一この規則だけが拾えた型。"""
    article = _article(["アークライト", "イト", "知育玩具"], name="アークライト ito(イト)レインボー")
    result = check_tag_granularity(article, _corpus(**{"知育玩具": 500, "アークライト": 3}))
    assert "fragment" in result.message


def test_shared_substring_tag_is_not_a_fragment():
    """「レゴ」⊂「レゴ シティ」は断片ではない (レゴは 89 記事を束ねている)。"""
    article = _article(["レゴ", "レゴ シティ", "知育玩具"], name="レゴ シティ 消防署")
    result = check_tag_granularity(article, _corpus(**{"レゴ": 89, "知育玩具": 500, "レゴシティ": 3}))
    assert "fragment" not in result.message


def test_unshared_ratio_warns_above_threshold():
    tags = ["固有A", "固有B", "固有C", "固有D", "知育玩具"]
    result = check_tag_granularity(_article(tags), SHARED)
    assert "unshared" in result.message
    assert result.score < 1.0


def test_unshared_ratio_at_median_does_not_warn():
    """中央値 (5 個中 1 個 = 20%) は選別しない。"""
    tags = ["固有A", "知育玩具", "プレゼント", "ブロック", "レゴ"]
    result = check_tag_granularity(_article(tags), SHARED)
    assert result.score == 1.0
    assert result.message.startswith("OK")


def test_threshold_is_above_the_measured_median():
    assert TAG_NOVEL_RATIO_WARN >= 0.6


def test_tag_appearing_in_exactly_one_other_article_is_shared():
    result = check_tag_granularity(_article(["ニッチ語", "知育玩具"]), _corpus(**{"ニッチ語": 1, "知育玩具": 500}))
    assert result.message.startswith("OK")


def test_corpus_none_skips_only_the_ratio_rule():
    article = _article(["ジスター 天才のはじまり", "固有A", "固有B"])
    result = check_tag_granularity(article, None)
    assert "product-name" in result.message
    assert "unshared" not in result.message


def test_score_floor():
    tags = ["ジスター 天才のはじまり", "ジスター 天才のはじまり ブロック", "固有A", "固有B", "固有C"]
    result = check_tag_granularity(_article(tags), SHARED)
    assert result.score >= 0.5


def test_load_tag_corpus_counts_articles_not_occurrences(tmp_path):
    for i, tags in enumerate([["レゴ", "知育玩具"], ["レゴ", "プレゼント"]]):
        (tmp_path / f"2026-08-1{i}-B0X.json").write_text(
            json.dumps({"tags": tags}, ensure_ascii=False), encoding="utf-8"
        )
    # 同一記事内の重複タグは 1 と数える
    (tmp_path / "2026-08-13-B0Y.json").write_text(
        json.dumps({"tags": ["レゴ", "レゴ"]}, ensure_ascii=False), encoding="utf-8"
    )
    corpus = load_tag_corpus(tmp_path)
    assert corpus["レゴ"] == 3
    assert corpus["知育玩具"] == 1


def test_load_tag_corpus_skips_sidecars_and_broken_json(tmp_path):
    (tmp_path / "2026-08-10-B0X.json").write_text(
        json.dumps({"tags": ["レゴ"]}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "2026-08-10-B0X.quality.json").write_text(
        json.dumps({"tags": ["混入してはいけない"]}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "2026-08-11-B0Z.json").write_text("{ broken", encoding="utf-8")
    corpus = load_tag_corpus(tmp_path)
    assert corpus["レゴ"] == 1
    assert "混入してはいけない" not in corpus


def test_load_tag_corpus_returns_none_when_missing(tmp_path):
    assert load_tag_corpus(tmp_path / "nope") is None
    assert load_tag_corpus(tmp_path) is None
