"""#7569 型6 probe (ASIN リンク近傍の一人称マーカー) の検査。

ネットワークは一切叩かない (fetch_fn を注入)。
"""
from __future__ import annotations

import json
import pathlib

from scripts.probe_first_party_link_proximity import (
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    aggregate_by_asin,
    build_asin_posts,
    build_link_to_post_id,
    build_pairs,
    collect_post_ids,
    count_markers_in_text,
    distribution_stats,
    extract_windows,
    fetch_contents,
    find_asin_link_spans,
    full_drop_specificity,
    full_recall_specificity,
    run,
    score_asin_in_post,
    score_pairs,
)

# --------------------------------------------------------------------------
# 窓の切り出し
# --------------------------------------------------------------------------

def test_find_asin_link_spans_matches_dp_pattern():
    html = '<a href="https://www.amazon.co.jp/dp/B012345678">buy</a>'
    spans = find_asin_link_spans(html, "B012345678")
    assert len(spans) == 1
    start, end = spans[0]
    assert html[start:end] == "/dp/B012345678"


def test_find_asin_link_spans_is_case_insensitive_but_normalizes_upper():
    html = '<a href="/dp/b012345678">buy</a>'
    assert len(find_asin_link_spans(html, "B012345678")) == 1


def test_find_asin_link_spans_filters_by_target_asin():
    html = '<a href="/dp/B0AAAAAAAA">a</a> <a href="/dp/B0BBBBBBBB">b</a>'
    assert len(find_asin_link_spans(html, "B0AAAAAAAA")) == 1
    assert len(find_asin_link_spans(html, "B0BBBBBBBB")) == 1
    assert find_asin_link_spans(html, "B0CCCCCCCC") == []


def test_find_asin_link_spans_matches_gp_product_and_asin_query():
    html = (
        '<a href="/gp/product/B0AAAAAAAA">a</a> '
        '<a href="/x?asin=B0AAAAAAAA&y=1">b</a>'
    )
    assert len(find_asin_link_spans(html, "B0AAAAAAAA")) == 2


def test_find_asin_link_spans_empty_or_non_string_is_safe():
    assert find_asin_link_spans("", "B0AAAAAAAA") == []
    assert find_asin_link_spans(None, "B0AAAAAAAA") == []  # type: ignore[arg-type]


def test_extract_windows_clamps_at_string_boundaries():
    html = "x" * 5 + "<a href=\"/dp/B0AAAAAAAA\">link</a>" + "y" * 5
    windows = extract_windows(html, "B0AAAAAAAA", window_chars=1000)
    assert len(windows) == 1
    assert "xxxxx" in windows[0]
    assert "yyyyy" in windows[0]


def test_extract_windows_respects_window_size():
    prefix = "うちの子が" + "z" * 300
    html = prefix + '<a href="/dp/B0AAAAAAAA">link</a>'
    narrow = extract_windows(html, "B0AAAAAAAA", window_chars=50)
    wide = extract_windows(html, "B0AAAAAAAA", window_chars=1000)
    assert "うちの子が" not in narrow[0]
    assert "うちの子が" in wide[0]


def test_extract_windows_no_match_returns_empty_list():
    assert extract_windows("<p>no links here</p>", "B0AAAAAAAA", 500) == []


# --------------------------------------------------------------------------
# マーカー計数
# --------------------------------------------------------------------------

def test_count_markers_in_text_sums_all_marker_types():
    text = "うちの子が使ってみた。実際に買ってよかった。"
    # うちの子(1) + 使って(1) + 買って(1) + 実際に(1) = 4
    assert count_markers_in_text(text) == 4


def test_count_markers_in_text_zero_when_absent():
    assert count_markers_in_text("これは参考商品です。") == 0


def test_score_asin_in_post_combines_occurrences_max_sum():
    html = (
        'うちの子が大好き<a href="/dp/B0AAAAAAAA">A</a>普通のリンク'
        '参考として<a href="/dp/B0AAAAAAAA">A</a>だけ'
    )
    score = score_asin_in_post(html, "B0AAAAAAAA", window_chars=20)
    assert score["occurrences"] == 2
    assert score["max"] >= 1
    assert score["sum"] >= score["max"]


# --------------------------------------------------------------------------
# ラベル付き集合 <-> post_id の対応付け
# --------------------------------------------------------------------------

def test_build_asin_posts_filters_role_and_asin_set():
    sources = [
        {"asin": "B0POS00001", "role": "primary", "post_url": "https://omcha.jp/p1/"},
        {"asin": "B0POS00001", "role": "compared", "post_url": "https://omcha.jp/p2/"},
        {"asin": "B0NEG00001", "role": "primary", "post_url": "https://omcha.jp/p3/"},
        {"asin": "B0OTHER001", "role": "primary", "post_url": "https://omcha.jp/p4/"},
    ]
    out = build_asin_posts(sources, {"B0POS00001", "B0NEG00001"})
    assert out == {
        "B0POS00001": ["https://omcha.jp/p1/"],
        "B0NEG00001": ["https://omcha.jp/p3/"],
    }


def test_build_asin_posts_dedupes_repeated_post_url():
    sources = [
        {"asin": "B0X", "role": "primary", "post_url": "https://omcha.jp/p1/"},
        {"asin": "B0X", "role": "primary", "post_url": "https://omcha.jp/p1/"},
    ]
    assert build_asin_posts(sources, {"B0X"}) == {"B0X": ["https://omcha.jp/p1/"]}


def test_build_link_to_post_id_reads_link_field():
    posts_cache = {"101": {"link": "https://omcha.jp/p1/"}, "102": {"link": "https://omcha.jp/p2/"}}
    assert build_link_to_post_id(posts_cache) == {
        "https://omcha.jp/p1/": "101", "https://omcha.jp/p2/": "102",
    }


def test_collect_post_ids_drops_unmatched_urls():
    asin_posts = {"B0X": ["https://omcha.jp/p1/", "https://omcha.jp/missing/"]}
    link_to_id = {"https://omcha.jp/p1/": "101"}
    assert collect_post_ids(asin_posts, link_to_id) == {"B0X": ["101"]}


# --------------------------------------------------------------------------
# 本文取得 (fetch_fn を注入。ネットワークを叩かない)
# --------------------------------------------------------------------------

def test_fetch_contents_uses_injected_fetch_fn_and_sleeps_between_not_after():
    calls = []
    slept = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return f"<p>content {post_id}</p>"

    contents, failed = fetch_contents(
        ["101", "102", "103"], "https://omcha.jp", session=object(),
        sleep_seconds=1.0, limit=0, sleeper=slept.append, fetch_fn=fake_fetch,
    )
    assert calls == [101, 102, 103]
    assert contents == {"101": "<p>content 101</p>", "102": "<p>content 102</p>",
                        "103": "<p>content 103</p>"}
    assert failed == []
    assert slept == [1.0, 1.0]


def test_fetch_contents_limit_caps_requests():
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return "<p>ok</p>"

    contents, failed = fetch_contents(
        ["103", "101", "102"], "https://omcha.jp", session=object(),
        sleep_seconds=0, limit=2, sleeper=lambda s: None, fetch_fn=fake_fetch,
    )
    assert calls == [101, 102]  # 数値順にソートしてから limit
    assert set(contents) == {"101", "102"}


def test_fetch_contents_records_failures_without_raising():
    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return None if post_id == 101 else "<p>ok</p>"

    contents, failed = fetch_contents(
        ["101", "102"], "https://omcha.jp", session=object(),
        sleep_seconds=0, limit=0, sleeper=lambda s: None, fetch_fn=fake_fetch,
    )
    assert failed == ["101"]
    assert set(contents) == {"102"}


# --------------------------------------------------------------------------
# ペア構築・スコアリング・集計
# --------------------------------------------------------------------------

def test_build_pairs_skips_posts_without_content():
    asin_post_ids = {"B0X": ["101", "102"]}
    contents = {"101": '<a href="/dp/B0X">x</a>'}  # 102 は未取得
    pairs = build_pairs(asin_post_ids, contents)
    assert len(pairs) == 1
    assert pairs[0]["post_id"] == "101"


def test_score_pairs_zero_when_link_not_found():
    pairs = [{"asin": "B0X", "post_id": "101", "content": "<p>no link</p>", "spans": []}]
    scored = score_pairs(pairs, window_chars=200)
    assert scored[0] == {"asin": "B0X", "post_id": "101", "occurrences": 0, "max": 0, "sum": 0}


def test_aggregate_by_asin_takes_max_of_max_and_sum_of_sum_across_posts():
    scored = [
        {"asin": "B0X", "post_id": "101", "occurrences": 1, "max": 2, "sum": 2},
        {"asin": "B0X", "post_id": "102", "occurrences": 2, "max": 5, "sum": 7},
    ]
    agg = aggregate_by_asin(scored, ("B0X", "B0Y"))
    assert agg["B0X"] == {"pair_count": 2, "max": 5, "sum": 9}
    assert agg["B0Y"] == {"pair_count": 0, "max": None, "sum": None}


# --------------------------------------------------------------------------
# 分布・閾値分析
# --------------------------------------------------------------------------

def test_distribution_stats_basic():
    stats = distribution_stats([1, 2, 3, 4])
    assert stats["n"] == 4
    assert stats["min"] == 1
    assert stats["max"] == 4
    assert stats["median"] == 2.5


def test_distribution_stats_empty():
    assert distribution_stats([]) == {"n": 0, "min": None, "q1": None, "median": None,
                                       "q3": None, "max": None}


def test_distribution_stats_single_value():
    stats = distribution_stats([7])
    assert stats["n"] == 1
    assert stats["min"] == stats["max"] == stats["median"] == stats["q1"] == stats["q3"] == 7


def test_full_recall_specificity_drops_negatives_below_min_positive():
    pos = [3, 5, 8]
    neg = [1, 2, 4, 9]
    r = full_recall_specificity(pos, neg)
    assert r == {"threshold": 3, "neg_dropped": 2, "neg_total": 4}  # 1,2 < 3


def test_full_recall_specificity_empty_positive_is_none():
    assert full_recall_specificity([], [1, 2]) is None


def test_full_drop_specificity_keeps_positives_above_max_negative():
    pos = [3, 5, 8]
    neg = [1, 2, 4]
    r = full_drop_specificity(pos, neg)
    assert r == {"neg_ceiling": 4, "pos_kept": 2, "pos_total": 3}  # 5,8 > 4


def test_full_drop_specificity_empty_negative_is_none():
    assert full_drop_specificity([1, 2], []) is None


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

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        # 正例には強いマーカーを近くに、負例には遠くに置く (分離できるはずの合成データ)
        return '<p>うちの子が使ってみた</p><a href="/dp/B0AAAAAAAA">x</a>'

    report = run(
        sources_path=sources_path, out_path=out_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert out_path.exists()
    assert not (tmp_path / "data").exists()
    assert set(report["windows"].keys()) == {"200", "500", "1000"}
    for w in report["windows"].values():
        assert set(w.keys()) == {"max", "sum"}
        for kind in ("max", "sum"):
            assert w[kind]["positive"]["n"] == len(POSITIVE_ASINS)
            assert w[kind]["negative"]["n"] == len(NEGATIVE_ASINS)


def test_run_reports_fetch_failures_and_zero_link_pairs(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        if post_id % 2 == 0:
            return None
        return "<p>no asin link at all</p>"

    report = run(
        sources_path=sources_path, out_path=out_path, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(report["fetch_failed_posts"]) > 0
    assert report["zero_link_pairs"] > 0


def test_run_limit_caps_fetched_posts(tmp_path):
    sources_path = _sources_payload(tmp_path)
    out_path = tmp_path / "probe.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return "<p>x</p>"

    run(
        sources_path=sources_path, out_path=out_path, limit=3,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(calls) == 3
