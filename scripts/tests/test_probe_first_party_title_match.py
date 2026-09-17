"""#7569 型6 probe 第3弾 (A) (記事タイトルと商品名の一致) の検査。

ネットワークは一切叩かない (fetch_fn を注入)。HTML はすべて固定文字列。
"""
from __future__ import annotations

import json
import pathlib

from scripts.probe_first_party_title_match import (
    METRICS,
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    aggregate_by_asin,
    best_scores_for_asin,
    bigram_jaccard,
    build_names_dump,
    build_pairs,
    build_post_id_to_url,
    build_post_titles,
    build_report,
    candidate_texts_from_node,
    char_bigrams,
    example_rows,
    extract_product_name_candidates,
    find_block_ancestor,
    is_cta_text,
    longest_common_substring_len,
    match_scores,
    normalize_for_match,
    run,
)

# --------------------------------------------------------------------------
# 正規化
# --------------------------------------------------------------------------

def test_normalize_nfkc_folds_fullwidth_alnum_to_halfwidth():
    assert normalize_for_match("Ａ１") == "a1"


def test_normalize_lowercases():
    assert normalize_for_match("ABC") == "abc"


def test_normalize_strips_brackets_and_symbols():
    assert normalize_for_match("【ロジカルルートパズル】口コミ！") == "ロジカルルートパズル口コミ"


def test_normalize_strips_middle_dot_and_slash():
    assert normalize_for_match("エド・インター 型はめ/パズル") == "エドインター型はめパズル"


def test_normalize_keeps_katakana_long_vowel_mark():
    # ー (U+30FC) は \p{Katakana} に一致しない既知の落とし穴。除去されないことを確認する。
    assert normalize_for_match("ジスター") == "ジスター"


def test_normalize_empty_or_non_string_is_safe():
    assert normalize_for_match("") == ""
    assert normalize_for_match(None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 文字 n-gram 指標
# --------------------------------------------------------------------------

def test_longest_common_substring_len_basic():
    assert longest_common_substring_len("あいうえお", "うえ") == 2


def test_longest_common_substring_len_no_overlap():
    assert longest_common_substring_len("abc", "xyz") == 0


def test_longest_common_substring_len_empty_inputs():
    assert longest_common_substring_len("", "abc") == 0
    assert longest_common_substring_len("abc", "") == 0


def test_char_bigrams_basic():
    assert char_bigrams("abc") == {"ab", "bc"}


def test_char_bigrams_single_char_returns_itself():
    assert char_bigrams("a") == {"a"}


def test_char_bigrams_empty_returns_empty_set():
    assert char_bigrams("") == set()


def test_bigram_jaccard_identical_strings_is_one():
    assert bigram_jaccard("abcde", "abcde") == 1.0


def test_bigram_jaccard_disjoint_strings_is_zero():
    assert bigram_jaccard("abc", "xyz") == 0.0


def test_bigram_jaccard_both_empty_is_zero():
    assert bigram_jaccard("", "") == 0.0


def test_match_scores_full_containment():
    scores = match_scores("ジスター", "【ジスター口コミ】徹底レビュー")
    assert scores["lcs_len"] == 4
    assert scores["lcs_ratio"] == 1.0
    assert scores["bigram_jaccard"] > 0


def test_best_scores_for_asin_takes_max_per_metric():
    scores = [
        {"lcs_len": 2, "lcs_ratio": 0.5, "bigram_jaccard": 0.1},
        {"lcs_len": 5, "lcs_ratio": 0.2, "bigram_jaccard": 0.9},
    ]
    best = best_scores_for_asin(scores)
    assert best == {"lcs_len": 5, "lcs_ratio": 0.5, "bigram_jaccard": 0.9}


def test_best_scores_for_asin_empty_list_is_none():
    assert best_scores_for_asin([]) is None


# --------------------------------------------------------------------------
# 商品名候補の抽出 (Cocoon 商品ボックス / pochipp)
# --------------------------------------------------------------------------

def test_extract_product_name_candidates_cocoon_box_uses_title_attribute_and_text():
    html = (
        '<div class="amazon-item-box product-item-box B0AAAAAAAA">'
        '<figure class="amazon-item-thumb product-item-thumb">'
        '<a class="amazon-item-thumb-link product-item-thumb-link" '
        'href="/dp/B0AAAAAAAA" title="テスト商品X 型番123"><img alt=""></a>'
        '<a class="swatchimages" href="/dp/B0AAAAAAAA"><img alt=""></a>'
        '</figure>'
        '<div class="amazon-item-content">'
        '<div class="amazon-item-title">'
        '<a class="amazon-item-title-link" href="/dp/B0AAAAAAAA" title="テスト商品X 型番123">'
        'テスト商品X 型番123</a></div>'
        '<div class="amazon-item-buttons"><div class="shoplinkamazon">'
        '<a href="/dp/B0AAAAAAAA">Amazon で価格を見る ＞</a></div></div>'
        '</div></div>'
    )
    candidates = extract_product_name_candidates(html, "B0AAAAAAAA")
    assert candidates == ["テスト商品X 型番123"]


def test_extract_product_name_candidates_pochipp_box_uses_title_block_text():
    html = (
        '<div class="pochipp-box">'
        '<div class="pochipp-box__image"><a href="/dp/B0BBBBBBBB"><img alt=""></a></div>'
        '<div class="pochipp-box__title"><a href="/dp/B0BBBBBBBB">テスト商品Y</a></div>'
        '<div class="pochipp-box__btnwrap -amazon">'
        '<a href="/dp/B0BBBBBBBB">Amazon で価格を見る ＞</a></div>'
        '</div>'
    )
    candidates = extract_product_name_candidates(html, "B0BBBBBBBB")
    assert candidates == ["テスト商品Y"]


def test_extract_product_name_candidates_skips_non_block_occurrences():
    html = '<p>地の文の中で<a href="/dp/B0CCCCCCCC">これ</a>を紹介します</p>'
    assert extract_product_name_candidates(html, "B0CCCCCCCC") == []


def test_extract_product_name_candidates_dedupes_and_preserves_order():
    html = (
        '<div class="amazon-item-box">'
        '<a class="amazon-item-thumb-link" href="/dp/B0DDDDDDDD" title="重複商品"><img alt=""></a>'
        '<div class="amazon-item-title"><a href="/dp/B0DDDDDDDD" title="重複商品">重複商品</a></div>'
        '</div>'
    )
    candidates = extract_product_name_candidates(html, "B0DDDDDDDD")
    assert candidates == ["重複商品"]


def test_is_cta_text_matches_known_button_phrases():
    assert is_cta_text("Amazon で価格を見る ＞")
    assert is_cta_text("楽天市場 で最安値をチェック ＞")
    assert is_cta_text("口コミを見る")
    assert not is_cta_text("ジスター 天才のはじまり")


def test_find_block_ancestor_returns_none_when_no_block_marker():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<p><a href="/dp/B0EEEEEEEE">x</a></p>', "html.parser")
    a_tag = soup.find("a")
    assert find_block_ancestor(a_tag) is None


def test_candidate_texts_from_node_filters_empty_and_cta():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(
        '<div class="shoplinkamazon"><a href="/dp/B0X">Amazon で価格を見る ＞</a></div>',
        "html.parser",
    )
    node = soup.find("div")
    assert candidate_texts_from_node(node) == []


# --------------------------------------------------------------------------
# post_title の引き当て
# --------------------------------------------------------------------------

def test_build_post_titles_keys_by_asin_and_post_url():
    sources = [
        {"asin": "B0X", "post_url": "https://omcha.jp/x/", "post_title": "タイトルX"},
        {"asin": "B0Y", "post_url": "https://omcha.jp/y/", "post_title": None},
    ]
    titles = build_post_titles(sources)
    assert titles == {("B0X", "https://omcha.jp/x/"): "タイトルX"}


def test_build_post_id_to_url_reads_link_field():
    posts_cache = {"101": {"link": "https://omcha.jp/a/"}, "102": {"nope": 1}}
    assert build_post_id_to_url(posts_cache) == {"101": "https://omcha.jp/a/"}


# --------------------------------------------------------------------------
# ペア構築・集計
# --------------------------------------------------------------------------

def test_build_pairs_scores_candidates_against_post_title():
    asin_post_ids = {"B0AAAAAAAA": ["101"]}
    contents = {
        "101": (
            '<div class="amazon-item-box">'
            '<div class="amazon-item-title"><a href="/dp/B0AAAAAAAA">ジスター 天才のはじまり</a></div>'
            '</div>'
        )
    }
    post_id_to_url = {"101": "https://omcha.jp/x/"}
    post_titles = {("B0AAAAAAAA", "https://omcha.jp/x/"): "【ジスター口コミ】徹底レビュー"}
    pairs = build_pairs(asin_post_ids, contents, post_id_to_url, post_titles)
    assert len(pairs) == 1
    assert pairs[0]["candidates"] == ["ジスター 天才のはじまり"]
    assert len(pairs[0]["candidate_scores"]) == 1
    assert pairs[0]["candidate_scores"][0]["lcs_len"] > 0


def test_build_pairs_skips_posts_without_content():
    asin_post_ids = {"B0AAAAAAAA": ["101", "102"]}
    contents = {"101": '<div class="amazon-item-box"><a href="/dp/B0AAAAAAAA">x</a></div>'}
    pairs = build_pairs(asin_post_ids, contents, {}, {})
    assert len(pairs) == 1
    assert pairs[0]["post_id"] == "101"


def test_build_pairs_handles_missing_post_title():
    asin_post_ids = {"B0AAAAAAAA": ["101"]}
    contents = {"101": '<div class="amazon-item-box"><a href="/dp/B0AAAAAAAA" title="商品名">x</a></div>'}
    pairs = build_pairs(asin_post_ids, contents, {"101": "https://omcha.jp/x/"}, {})
    assert pairs[0]["post_title"] is None
    assert pairs[0]["candidate_scores"] == []


def test_aggregate_by_asin_takes_max_across_posts_and_counts_no_candidate():
    pairs = [
        {"asin": "B0X", "post_id": "101", "post_title": "T1", "candidates": ["a"],
         "candidate_scores": [{"lcs_len": 2, "lcs_ratio": 0.5, "bigram_jaccard": 0.1}]},
        {"asin": "B0X", "post_id": "102", "post_title": "T2", "candidates": [],
         "candidate_scores": []},
    ]
    agg = aggregate_by_asin(pairs, ("B0X", "B0Y"))
    assert agg["B0X"]["pair_count"] == 2
    assert agg["B0X"]["no_candidate_pairs"] == 1
    assert agg["B0X"]["missing_post_title_pairs"] == 0
    assert agg["B0X"]["scores"] == {"lcs_len": 2, "lcs_ratio": 0.5, "bigram_jaccard": 0.1}
    assert agg["B0Y"] == {
        "pair_count": 0, "no_candidate_pairs": 0, "missing_post_title_pairs": 0, "scores": None,
    }


def test_example_rows_picks_pairs_with_candidates_and_title():
    pairs = [
        {"asin": "B0X", "post_id": "101", "post_title": "T1", "candidates": ["cand"],
         "candidate_scores": [{"lcs_len": 3, "lcs_ratio": 1.0, "bigram_jaccard": 1.0}]},
    ]
    examples = example_rows(pairs, ("B0X", "B0Y"))
    assert len(examples) == 1
    assert examples[0]["asin"] == "B0X"
    assert examples[0]["product_name_candidate"] == "cand"


def test_build_report_metrics_cover_all_three_metrics():
    agg = {a: {
        "pair_count": 1, "no_candidate_pairs": 0, "missing_post_title_pairs": 0,
        "scores": {"lcs_len": 3, "lcs_ratio": 0.5, "bigram_jaccard": 0.2},
    } for a in POSITIVE_ASINS + NEGATIVE_ASINS}
    report = build_report(agg, [], [], 0)
    assert set(report["metrics"].keys()) == set(METRICS)
    assert report["metrics"]["lcs_len"]["positive"]["n"] == len(POSITIVE_ASINS)
    assert report["metrics"]["lcs_len"]["negative"]["n"] == len(NEGATIVE_ASINS)


def test_build_names_dump_lists_pairs():
    pairs = [{"asin": "B0X", "post_id": "101", "post_title": "T", "candidates": ["c"],
              "candidate_scores": []}]
    dump = build_names_dump(pairs)
    assert dump["pairs"] == [{"asin": "B0X", "post_id": "101", "post_title": "T", "candidates": ["c"]}]


# --------------------------------------------------------------------------
# run (統合。fetch_fn を注入しネットワークを叩かない)
# --------------------------------------------------------------------------

def _sources_payload(tmp_path: pathlib.Path) -> pathlib.Path:
    sources = [
        {
            "asin": a, "role": "primary",
            "post_url": f"https://omcha.jp/{a.lower()}/",
            "post_title": f"タイトル {a}",
        }
        for a in list(POSITIVE_ASINS) + list(NEGATIVE_ASINS)
    ]
    posts_cache = {
        str(100 + i): {"link": f"https://omcha.jp/{a.lower()}/"}
        for i, a in enumerate(list(POSITIVE_ASINS) + list(NEGATIVE_ASINS))
    }
    payload = {"sources": sources, "posts_cache": posts_cache}
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_run_never_touches_data_dir_and_writes_report(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "out" / "probe.json"
    cache_path = tmp_path / "cache.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return '<div class="amazon-item-box"><a href="/dp/B0AAAAAAAA" title="商品名">x</a></div>'

    report = run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert out_path.exists()
    assert cache_path.exists()
    assert not (tmp_path / "data").exists()
    assert set(report["metrics"].keys()) == set(METRICS)


def test_run_reuses_cache_on_second_call_without_refetching(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return '<div class="amazon-item-box"><a href="/dp/B0AAAAAAAA" title="商品名">x</a></div>'

    run(sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object())
    assert len(calls) > 0

    def failing_fetch(post_id, wp_base_url, session, sleeper):
        raise AssertionError("キャッシュ済みの post を再取得しようとした")

    run(sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=failing_fetch, session=object())


def test_run_reports_no_candidate_pairs(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return "<p>商品ボックスが無い地の文だけの記事</p>"

    report = run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert report["total_pairs"] == len(POSITIVE_ASINS) + len(NEGATIVE_ASINS)
    assert report["no_candidate_pairs"] == report["total_pairs"]


def test_run_dump_names_writes_when_requested(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    dump_path = tmp_path / "names.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return '<div class="amazon-item-box"><a href="/dp/B0AAAAAAAA" title="商品名">x</a></div>'

    run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path,
        dump_names_path=dump_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert dump_path.exists()
    dumped = json.loads(dump_path.read_text(encoding="utf-8"))
    assert "pairs" in dumped


def test_run_limit_caps_fetched_posts(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return "<p>x</p>"

    run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=3,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(calls) == 3
