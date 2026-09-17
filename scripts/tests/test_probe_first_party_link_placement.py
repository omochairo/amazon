"""#7569 型6 probe 第2弾 (ASIN リンクの構造的な置き場所) の検査。

ネットワークは一切叩かない (fetch_fn を注入)。
"""
from __future__ import annotations

import json
import pathlib

from scripts.probe_first_party_link_placement import (
    CATEGORIES,
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    aggregate_by_asin,
    ancestor_chain,
    boolean_breakdown,
    build_ancestor_dump,
    build_pairs,
    build_report,
    categorize_occurrences,
    category_crosstab,
    classify_chain,
    fetch_contents_cached,
    find_asin_link_tags,
    load_cache,
    run,
    save_cache,
)

# --------------------------------------------------------------------------
# リンク検出・祖先チェーン
# --------------------------------------------------------------------------

def test_find_asin_link_tags_matches_dp_pattern():
    html = '<a href="https://www.amazon.co.jp/dp/B012345678">buy</a>'
    tags = find_asin_link_tags(html, "B012345678")
    assert len(tags) == 1
    assert tags[0].name == "a"


def test_find_asin_link_tags_is_case_insensitive():
    html = '<a href="/dp/b012345678">buy</a>'
    assert len(find_asin_link_tags(html, "B012345678")) == 1


def test_find_asin_link_tags_filters_by_target_asin():
    html = '<a href="/dp/B0AAAAAAAA">a</a> <a href="/dp/B0BBBBBBBB">b</a>'
    assert len(find_asin_link_tags(html, "B0AAAAAAAA")) == 1
    assert find_asin_link_tags(html, "B0CCCCCCCC") == []


def test_find_asin_link_tags_matches_gp_product_and_asin_query():
    html = (
        '<a href="/gp/product/B0AAAAAAAA">a</a> '
        '<a href="/x?asin=B0AAAAAAAA&y=1">b</a>'
    )
    assert len(find_asin_link_tags(html, "B0AAAAAAAA")) == 2


def test_find_asin_link_tags_empty_or_non_string_is_safe():
    assert find_asin_link_tags("", "B0AAAAAAAA") == []
    assert find_asin_link_tags(None, "B0AAAAAAAA") == []  # type: ignore[arg-type]


def test_ancestor_chain_excludes_document_and_includes_self():
    html = '<div class="outer"><p class="inner"><a class="link" href="/dp/B0AAAAAAAA">x</a></p></div>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    chain = ancestor_chain(tag)
    assert chain[0] == {"tag": "a", "classes": ["link"]}
    assert chain[1] == {"tag": "p", "classes": ["inner"]}
    assert chain[2] == {"tag": "div", "classes": ["outer"]}
    assert all(node["tag"] != "[document]" for node in chain)


def test_ancestor_chain_no_class_is_empty_list():
    html = '<p><a href="/dp/B0AAAAAAAA">x</a></p>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    chain = ancestor_chain(tag)
    assert chain[0]["classes"] == []


# --------------------------------------------------------------------------
# カテゴリ分類 (実測に基づく規則)
# --------------------------------------------------------------------------

def test_classify_chain_narrative_when_bare_p_wraps_link():
    html = '<p>うちの子が使ってみた<a href="/dp/B0AAAAAAAA">これ</a>を紹介します</p>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "narrative"


def test_classify_chain_narrative_skips_inline_wrappers_like_strong():
    html = '<p>参考: <strong><a href="/dp/B0AAAAAAAA">amazon商品ページ</a></strong>より</p>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "narrative"


def test_classify_chain_block_for_figure_ancestor():
    html = (
        '<div class="amazon-item-box"><figure class="amazon-item-thumb">'
        '<a href="/dp/B0AAAAAAAA">x</a></figure></div>'
    )
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "block"


def test_classify_chain_block_for_table_ancestor():
    html = '<table><tr><td><a href="/dp/B0AAAAAAAA">x</a></td></tr></table>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "block"


def test_classify_chain_block_for_pochipp_box_class():
    html = '<div class="pochipp-box__btnwrap -amazon"><a class="pochipp-box__btn" href="/dp/B0AAAAAAAA">x</a></div>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "block"


def test_classify_chain_block_when_anchor_itself_carries_box_class_and_no_ancestor_class():
    # 実測: image-only thumb variant は祖先に class を持たず、a 自身が amazon-item-* を持つ
    html = '<a class="amazon-item-thumb-link product-item-image-only" href="/dp/B0AAAAAAAA">x</a>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "block"


def test_classify_chain_heading_or_toc_for_h_tag_ancestor():
    html = '<h2><a href="/dp/B0AAAAAAAA">x</a></h2>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "heading_or_toc"


def test_classify_chain_heading_or_toc_for_toc_class_ancestor():
    html = '<div class="lwptoc"><a href="/dp/B0AAAAAAAA">x</a></div>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "heading_or_toc"


def test_classify_chain_other_for_bare_div_without_box_class():
    html = '<div style="display:inline-block"><div><div><a href="/dp/B0AAAAAAAA">x</a></div></div></div>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "other"


def test_classify_chain_other_for_li_without_box_class():
    html = '<ul><li><a href="/dp/B0AAAAAAAA">x</a></li></ul>'
    tag = find_asin_link_tags(html, "B0AAAAAAAA")[0]
    assert classify_chain(ancestor_chain(tag)) == "other"


def test_categorize_occurrences_returns_category_and_chain_per_occurrence():
    html = (
        '<p>地の文<a href="/dp/B0AAAAAAAA">x</a></p>'
        '<figure class="amazon-item-thumb"><a href="/dp/B0AAAAAAAA">y</a></figure>'
    )
    occs = categorize_occurrences(html, "B0AAAAAAAA")
    assert [o["category"] for o in occs] == ["narrative", "block"]
    assert occs[0]["chain"][0]["tag"] == "a"


# --------------------------------------------------------------------------
# キャッシュ
# --------------------------------------------------------------------------

def test_load_cache_missing_file_returns_empty_dict(tmp_path):
    assert load_cache(tmp_path / "nope.json") == {}


def test_save_and_load_cache_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    save_cache(path, {"101": "<p>x</p>"})
    assert load_cache(path) == {"101": "<p>x</p>"}


def test_fetch_contents_cached_skips_fetch_for_cached_ids():
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return f"<p>{post_id}</p>"

    cache = {"101": "<p>cached</p>"}
    contents, failed = fetch_contents_cached(
        ["101", "102"], "https://omcha.jp", session=object(),
        sleep_seconds=0, limit=0, cache=cache, sleeper=lambda s: None, fetch_fn=fake_fetch,
    )
    assert calls == [102]
    assert contents == {"101": "<p>cached</p>", "102": "<p>102</p>"}
    assert cache == {"101": "<p>cached</p>", "102": "<p>102</p>"}
    assert failed == []


def test_fetch_contents_cached_records_failures_without_caching():
    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return None

    cache: dict[str, str] = {}
    contents, failed = fetch_contents_cached(
        ["101"], "https://omcha.jp", session=object(),
        sleep_seconds=0, limit=0, cache=cache, sleeper=lambda s: None, fetch_fn=fake_fetch,
    )
    assert failed == ["101"]
    assert contents == {}
    assert cache == {}


def test_fetch_contents_cached_limit_caps_requests():
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return "<p>ok</p>"

    fetch_contents_cached(
        ["103", "101", "102"], "https://omcha.jp", session=object(),
        sleep_seconds=0, limit=2, cache={}, sleeper=lambda s: None, fetch_fn=fake_fetch,
    )
    assert calls == [101, 102]


# --------------------------------------------------------------------------
# ペア構築・集計
# --------------------------------------------------------------------------

def test_build_pairs_skips_posts_without_content():
    asin_post_ids = {"B0X": ["101", "102"]}
    contents = {"101": '<a href="/dp/B0X">x</a>'}  # 102 は未取得
    pairs = build_pairs(asin_post_ids, contents)
    assert len(pairs) == 1
    assert pairs[0]["post_id"] == "101"


def test_aggregate_by_asin_sums_categories_across_posts():
    pairs = [
        {"asin": "B0X", "post_id": "101", "occurrences": [
            {"category": "block", "chain": []}, {"category": "narrative", "chain": []},
        ]},
        {"asin": "B0X", "post_id": "102", "occurrences": [
            {"category": "block", "chain": []},
        ]},
    ]
    agg = aggregate_by_asin(pairs, ("B0X", "B0Y"))
    assert agg["B0X"]["pair_count"] == 2
    assert agg["B0X"]["total_occurrences"] == 3
    assert agg["B0X"]["block_count"] == 2
    assert agg["B0X"]["block_ratio"] == 2 / 3
    assert agg["B0X"]["narrative_only"] is False
    assert agg["B0Y"] == {
        "pair_count": 0, "total_occurrences": 0,
        "category_counts": {c: 0 for c in CATEGORIES},
        "block_count": 0, "block_ratio": None, "narrative_only": None,
    }


def test_aggregate_by_asin_narrative_only_true_when_no_block():
    pairs = [{"asin": "B0X", "post_id": "101", "occurrences": [{"category": "narrative", "chain": []}]}]
    agg = aggregate_by_asin(pairs, ("B0X",))
    assert agg["B0X"]["narrative_only"] is True


def test_boolean_breakdown_counts_true_false():
    assert boolean_breakdown([True, True, False]) == {"true": 2, "false": 1}


def test_category_crosstab_sums_across_asins():
    agg = {
        "B0X": {"category_counts": {"narrative": 1, "block": 2, "heading_or_toc": 0, "other": 0}},
        "B0Y": {"category_counts": {"narrative": 0, "block": 1, "heading_or_toc": 0, "other": 1}},
    }
    assert category_crosstab(agg, ("B0X", "B0Y")) == {
        "narrative": 1, "block": 3, "heading_or_toc": 0, "other": 1,
    }


def test_build_report_includes_category_definitions_and_metrics():
    agg = {a: {
        "pair_count": 1, "total_occurrences": 2,
        "category_counts": {"narrative": 0, "block": 2, "heading_or_toc": 0, "other": 0},
        "block_count": 2, "block_ratio": 1.0, "narrative_only": False,
    } for a in POSITIVE_ASINS + NEGATIVE_ASINS}
    report = build_report(agg, [], 0, len(POSITIVE_ASINS) + len(NEGATIVE_ASINS))
    assert set(report["category_definitions"].keys()) == set(CATEGORIES)
    assert set(report["metrics"].keys()) == {"block_count", "block_ratio", "narrative_only"}
    assert report["metrics"]["narrative_only"]["positive"] == {"true": 0, "false": len(POSITIVE_ASINS)}


def test_build_ancestor_dump_tallies_tags_and_classes():
    pairs = [{"asin": "B0X", "post_id": "101", "occurrences": [
        {"category": "block", "chain": [
            {"tag": "a", "classes": ["amazon-item-thumb-link"]},
            {"tag": "figure", "classes": ["amazon-item-thumb"]},
        ]},
    ]}]
    dump = build_ancestor_dump(pairs)
    assert len(dump["occurrences"]) == 1
    assert dump["occurrences"][0]["asin"] == "B0X"
    assert dump["tag_frequency"] == {"a": 1, "figure": 1}
    assert dump["class_frequency"] == {"amazon-item-thumb-link": 1, "amazon-item-thumb": 1}


# --------------------------------------------------------------------------
# run (統合。fetch_fn を注入しネットワークを叩かない)
# --------------------------------------------------------------------------

def _sources_payload(tmp_path: pathlib.Path) -> pathlib.Path:
    sources = [
        {"asin": a, "role": "primary", "post_url": f"https://omcha.jp/{a.lower()}/"}
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
        return '<figure class="amazon-item-thumb"><a href="/dp/B0AAAAAAAA">x</a></figure>'

    report = run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert out_path.exists()
    assert cache_path.exists()
    assert not (tmp_path / "data").exists()
    assert set(report["metrics"].keys()) == {"block_count", "block_ratio", "narrative_only"}
    assert report["metrics"]["block_count"]["positive"]["n"] == len(POSITIVE_ASINS)
    assert report["metrics"]["block_count"]["negative"]["n"] == len(NEGATIVE_ASINS)


def test_run_reuses_cache_on_second_call_without_refetching(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return '<p>x<a href="/dp/B0AAAAAAAA">y</a></p>'

    run(sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object())
    first_call_count = len(calls)
    assert first_call_count > 0

    def failing_fetch(post_id, wp_base_url, session, sleeper):
        raise AssertionError("キャッシュ済みの post を再取得しようとした")

    run(sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=failing_fetch, session=object())


def test_run_reports_fetch_failures_and_zero_link_pairs(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        if post_id % 2 == 0:
            return None
        return "<p>no asin link at all</p>"

    report = run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(report["fetch_failed_posts"]) > 0
    assert report["zero_link_pairs"] > 0


def test_run_dump_ancestors_writes_when_requested(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    dump_path = tmp_path / "ancestors.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return '<figure class="amazon-item-thumb"><a href="/dp/B0AAAAAAAA">x</a></figure>'

    run(
        sources_path=sources_path, out_path=out_path, cache_path=cache_path,
        dump_ancestors_path=dump_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert dump_path.exists()
    dumped = json.loads(dump_path.read_text(encoding="utf-8"))
    assert "occurrences" in dumped and "tag_frequency" in dumped


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
