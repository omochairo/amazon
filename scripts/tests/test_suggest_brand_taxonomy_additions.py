"""scripts/suggest_brand_taxonomy_additions.py unit tests (A-6, epic #1356)."""
from __future__ import annotations

from scripts.suggest_brand_taxonomy_additions import (
    covered_by_taxonomy,
    is_brand_like,
    suggest,
    tokenize,
)


def _make_gsc(by_query):
    return {"by_query": by_query, "range": {"start": "2026-05-26", "end": "2026-06-01"}}


def test_tokenize_splits_whitespace_and_brackets():
    assert tokenize("レゴ デュプロ 2歳") == ["レゴ", "デュプロ", "2歳"]
    assert tokenize("「タカラトミー」(BANDAI)") == ["タカラトミー", "BANDAI"]


def test_tokenize_keeps_internal_hyphen_and_middle_dot():
    assert tokenize("Fisher-Price ベイブレード・X") == ["Fisher-Price", "ベイブレード・X"]


def test_is_brand_like_accepts_katakana_and_alpha():
    assert is_brand_like("レゴ", 2)
    assert is_brand_like("MODEROID", 3)
    assert is_brand_like("Fisher-Price", 3)
    assert is_brand_like("ベイブレードX", 3)


def test_is_brand_like_rejects_kanji_and_hiragana():
    assert not is_brand_like("おもちゃ", 3)
    assert not is_brand_like("知育玩具", 3)
    assert not is_brand_like("子供向け", 3)


def test_is_brand_like_rejects_too_short():
    assert not is_brand_like("X", 3)
    assert not is_brand_like("AB", 3)


def test_is_brand_like_rejects_pure_digits():
    assert not is_brand_like("12345", 3)


def test_covered_by_taxonomy_direct():
    assert covered_by_taxonomy("レゴ", {"レゴ", "lego"})


def test_covered_by_taxonomy_substring_both_ways():
    # taxonomy term contains token
    assert covered_by_taxonomy("lego", {"lego(レゴ)", "bandai"})
    # token contains taxonomy term
    assert covered_by_taxonomy("beybladex", {"beyblade"})


def test_covered_by_taxonomy_not_covered():
    assert not covered_by_taxonomy("ロンビー", {"バンダイ", "lego"})


def test_suggest_surfaces_unmatched_token():
    gsc = _make_gsc(by_query=[
        {"query": "ロンビー 知育", "impressions": 100, "clicks": 0, "ctr": 0.0, "position": 5.0},
    ])
    result = suggest(gsc, {"バンダイ", "lego"})
    tokens = [c["token"] for c in result["candidates"]]
    assert "ロンビー" in tokens


def test_suggest_skips_covered_brands():
    gsc = _make_gsc(by_query=[
        {"query": "レゴ デュプロ", "impressions": 200, "clicks": 1, "ctr": 0.005, "position": 5.0},
    ])
    result = suggest(gsc, {"レゴ", "デュプロ"})
    assert result["candidates"] == []


def test_suggest_skips_stopword_tokens():
    # "プログラミング" は stopword、フィルタされる
    gsc = _make_gsc(by_query=[
        {"query": "プログラミング おもちゃ", "impressions": 200, "clicks": 0, "ctr": 0.0, "position": 5.0},
    ])
    result = suggest(gsc, {"レゴ"})
    assert result["candidates"] == []


def test_suggest_min_impressions_filter():
    gsc = _make_gsc(by_query=[
        {"query": "ロンビー", "impressions": 5, "clicks": 0, "ctr": 0.0, "position": 5.0},
    ])
    result = suggest(gsc, set())
    assert result["candidates"] == []


def test_suggest_aggregates_across_queries():
    gsc = _make_gsc(by_query=[
        {"query": "ロンビー 知育", "impressions": 100, "clicks": 0, "ctr": 0.0, "position": 5.0},
        {"query": "ロンビー レビュー", "impressions": 50, "clicks": 0, "ctr": 0.0, "position": 7.0},
    ])
    result = suggest(gsc, set())
    candidate = next(c for c in result["candidates"] if c["token"] == "ロンビー")
    assert candidate["impressions_sum"] == 150
    assert candidate["query_count"] == 2
    assert len(candidate["queries"]) == 2


def test_suggest_top_n_caps():
    by_query = [
        {"query": f"NewBrand{i}", "impressions": 100 - i, "clicks": 0, "ctr": 0.0, "position": 5.0}
        for i in range(20)
    ]
    result = suggest(_make_gsc(by_query), set(), top_n=3)
    assert len(result["candidates"]) == 3
    # 最も impressions の高い順
    assert result["candidates"][0]["impressions_sum"] == 100


def test_suggest_query_examples_cap():
    by_query = [
        {"query": f"Foo Brand{i}", "impressions": 50, "clicks": 0, "ctr": 0.0, "position": 5.0}
        for i in range(10)
    ]
    result = suggest(_make_gsc(by_query), set(), query_examples=3)
    foo = next(c for c in result["candidates"] if c["token"] == "Foo")
    assert len(foo["queries"]) == 3


def test_suggest_deduplicates_tokens_within_one_query():
    # 同一クエリ内で同 token が複数回出ても 1 回しか加算しない
    gsc = _make_gsc(by_query=[
        {"query": "ロンビー ロンビー ロンビー", "impressions": 80, "clicks": 0, "ctr": 0.0, "position": 5.0},
    ])
    result = suggest(gsc, set())
    candidate = next(c for c in result["candidates"] if c["token"] == "ロンビー")
    assert candidate["impressions_sum"] == 80
    assert candidate["query_count"] == 1
