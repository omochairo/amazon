"""#7569 型6 probe (A) をリークの無い本文ラベル評価セット (#7610) で測り直す probe の検査。

ネットワークは一切叩かない (fetch_fn を注入)。HTML はすべて固定文字列。
計測ロジック (抽出・正規化・指標) 自体は probe_first_party_title_match のテストで
既に検査済みなので、ここではラベル読み込み・unclear の除外・source 別集計・
run の配線だけを見る。
"""
from __future__ import annotations

import json
import pathlib

from scripts.probe_first_party_title_match_v2 import (
    METRICS,
    aggregate_by_asin,
    build_pairs,
    build_report,
    labeled_items,
    load_labels_payload,
    metric_report,
    negative_asins,
    positive_asins,
    resolve_items,
    run,
    subset_kept_at_ceiling,
)

# --------------------------------------------------------------------------
# ラベル読み込み
# --------------------------------------------------------------------------

def _labels_payload(items):
    return {"items": items}


def test_labeled_items_excludes_unclear():
    payload = _labels_payload([
        {"asin": "B0X", "label": "review", "source": "pool_random_sample", "post_url": "u1"},
        {"asin": "B0Y", "label": "mention", "source": "pool_random_sample", "post_url": "u2"},
        {"asin": "B0Z", "label": "unclear", "source": "pool_random_sample", "post_url": "u3"},
    ])
    items = labeled_items(payload)
    assert {i["asin"] for i in items} == {"B0X", "B0Y"}


def test_labeled_items_maps_review_to_positive_and_mention_to_negative():
    payload = _labels_payload([
        {"asin": "B0X", "label": "review", "source": "pool_random_sample", "post_url": "u1"},
        {"asin": "B0Y", "label": "mention", "source": "pool_random_sample", "post_url": "u2"},
    ])
    items = labeled_items(payload)
    outcomes = {i["asin"]: i["outcome"] for i in items}
    assert outcomes == {"B0X": "positive", "B0Y": "negative"}


def test_labeled_items_ignores_prior_title_label():
    payload = _labels_payload([
        {
            "asin": "B0X", "label": "review", "source": "pool_random_sample", "post_url": "u1",
            "prior_title_label": "title_labeled_ii",
        },
    ])
    items = labeled_items(payload)
    assert "prior_title_label" not in items[0]


def test_positive_and_negative_asins_sorted_and_deduped_by_label():
    items = labeled_items(_labels_payload([
        {"asin": "B0Y", "label": "review", "source": "pool_random_sample", "post_url": "u1"},
        {"asin": "B0X", "label": "mention", "source": "pool_random_sample", "post_url": "u2"},
    ]))
    assert positive_asins(items) == ("B0Y",)
    assert negative_asins(items) == ("B0X",)


def test_load_labels_payload_reads_json(tmp_path):
    p = tmp_path / "labels.json"
    p.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert load_labels_payload(p) == {"items": []}


# --------------------------------------------------------------------------
# post の引き当て
# --------------------------------------------------------------------------

def test_resolve_items_attaches_post_id_and_title():
    items = [{"asin": "B0X", "post_url": "https://omcha.jp/x/", "outcome": "positive", "source": "s"}]
    sources = [{"asin": "B0X", "post_url": "https://omcha.jp/x/", "post_title": "タイトルX"}]
    posts_cache = {"101": {"link": "https://omcha.jp/x/"}}
    resolved, missing_id, missing_title = resolve_items(items, sources, posts_cache)
    assert resolved == [{**items[0], "post_id": "101", "post_title": "タイトルX"}]
    assert missing_id == []
    assert missing_title == []


def test_resolve_items_reports_missing_post_id():
    items = [{"asin": "B0X", "post_url": "https://omcha.jp/x/", "outcome": "positive", "source": "s"}]
    resolved, missing_id, missing_title = resolve_items(items, [], {})
    assert resolved == []
    assert missing_id == ["B0X"]
    assert missing_title == []


def test_resolve_items_reports_missing_post_title_but_keeps_item():
    items = [{"asin": "B0X", "post_url": "https://omcha.jp/x/", "outcome": "positive", "source": "s"}]
    posts_cache = {"101": {"link": "https://omcha.jp/x/"}}
    resolved, missing_id, missing_title = resolve_items(items, [], posts_cache)
    assert len(resolved) == 1
    assert resolved[0]["post_title"] is None
    assert missing_id == []
    assert missing_title == ["B0X"]


# --------------------------------------------------------------------------
# 測定・集計
# --------------------------------------------------------------------------

def test_build_pairs_scores_candidates_against_post_title():
    resolved = [{
        "asin": "B0AAAAAAAA", "post_id": "101", "post_title": "【ジスター口コミ】徹底レビュー",
        "outcome": "positive", "source": "s",
    }]
    contents = {
        "101": (
            '<div class="amazon-item-box">'
            '<div class="amazon-item-title"><a href="/dp/B0AAAAAAAA">ジスター 天才のはじまり</a></div>'
            '</div>'
        )
    }
    pairs = build_pairs(resolved, contents)
    assert len(pairs) == 1
    assert pairs[0]["candidates"] == ["ジスター 天才のはじまり"]
    assert pairs[0]["candidate_scores"][0]["lcs_len"] > 0
    assert pairs[0]["fetch_failed"] is False


def test_build_pairs_marks_fetch_failed_when_content_missing():
    resolved = [{"asin": "B0X", "post_id": "101", "post_title": "T", "outcome": "positive", "source": "s"}]
    pairs = build_pairs(resolved, {})
    assert pairs[0]["fetch_failed"] is True
    assert pairs[0]["candidates"] is None


def test_aggregate_by_asin_flags_no_candidate_only_when_title_present_and_fetch_ok():
    resolved = [
        {"asin": "B0A", "post_id": "1", "post_title": "T", "outcome": "positive", "source": "s"},
        {"asin": "B0B", "post_id": "2", "post_title": None, "outcome": "positive", "source": "s"},
    ]
    contents = {"1": "<p>商品ボックスが無い</p>", "2": "<p>商品ボックスが無い</p>"}
    pairs = build_pairs(resolved, contents)
    agg = aggregate_by_asin(pairs)
    assert agg["B0A"]["no_candidate"] is True
    assert agg["B0A"]["missing_post_title"] is False
    assert agg["B0B"]["no_candidate"] is False  # post_title が無いので candidate 欠測の対象外
    assert agg["B0B"]["missing_post_title"] is True


def test_subset_kept_at_ceiling_counts_values_above_ceiling():
    agg = {
        "B0A": {"scores": {"lcs_ratio": 0.5}},
        "B0B": {"scores": {"lcs_ratio": 0.1}},
        "B0C": {"scores": None},
    }
    result = subset_kept_at_ceiling(agg, ("B0A", "B0B", "B0C"), "lcs_ratio", ceiling=0.2)
    assert result == {"n": 2, "kept": 1}


def test_subset_kept_at_ceiling_none_ceiling_returns_none():
    assert subset_kept_at_ceiling({}, (), "lcs_ratio", None) is None


def test_metric_report_computes_full_recall_and_full_drop():
    agg = {
        "B0P": {"scores": {"lcs_ratio": 0.5}},
        "B0N": {"scores": {"lcs_ratio": 0.1}},
    }
    report = metric_report(agg, ("B0P",), ("B0N",), "lcs_ratio")
    assert report["positive"]["n"] == 1
    assert report["negative"]["n"] == 1
    assert report["full_recall_specificity"]["neg_dropped"] == 1
    assert report["full_drop_specificity"]["pos_kept"] == 1


def test_build_report_includes_source_breakdown_split_by_source():
    agg = {
        "B0CAL": {"scores": {"lcs_len": 5, "lcs_ratio": 0.5, "bigram_jaccard": 0.5}, "no_candidate": False},
        "B0POOL": {"scores": {"lcs_len": 4, "lcs_ratio": 0.4, "bigram_jaccard": 0.4}, "no_candidate": False},
        "B0NEG": {"scores": {"lcs_len": 1, "lcs_ratio": 0.05, "bigram_jaccard": 0.05}, "no_candidate": False},
    }
    resolved_items = [
        {"asin": "B0CAL", "source": "calibration_positive"},
        {"asin": "B0POOL", "source": "pool_random_sample"},
        {"asin": "B0NEG", "source": "pool_random_sample"},
    ]
    report = build_report(
        agg=agg, resolved_items=resolved_items,
        pos_asins=("B0CAL", "B0POOL"), neg_asins=("B0NEG",),
        missing_post_id=[], missing_post_title=[], fetch_failed_post_ids=[], cache_miss_asins=[],
        total_pairs=3, labels_path=pathlib.Path("labels.json"),
    )
    assert set(report["metrics"].keys()) == set(METRICS)
    breakdown = report["metrics"]["lcs_ratio"]["source_breakdown"]
    assert breakdown["calibration_positive"] == {"n": 1, "kept": 1}
    assert breakdown["pool_random_sample_positive"] == {"n": 1, "kept": 1}


# --------------------------------------------------------------------------
# run (統合。fetch_fn を注入しネットワークを叩かない)
# --------------------------------------------------------------------------

def _write_json(path: pathlib.Path, data) -> pathlib.Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _labels_and_sources(tmp_path: pathlib.Path):
    items = [
        {"asin": "B0POS1", "label": "review", "source": "calibration_positive", "post_url": "https://omcha.jp/p1/"},
        {"asin": "B0POS2", "label": "review", "source": "pool_random_sample", "post_url": "https://omcha.jp/p2/"},
        {"asin": "B0NEG1", "label": "mention", "source": "pool_random_sample", "post_url": "https://omcha.jp/n1/"},
        {"asin": "B0UNCLEAR", "label": "unclear", "source": "pool_random_sample", "post_url": "https://omcha.jp/u1/"},
    ]
    labels_path = _write_json(tmp_path / "labels.json", {"items": items})

    sources = [
        {"asin": row["asin"], "post_url": row["post_url"], "post_title": f"タイトル {row['asin']}"}
        for row in items
    ]
    posts_cache = {
        str(100 + i): {"link": row["post_url"]} for i, row in enumerate(items)
    }
    sources_path = _write_json(tmp_path / "sources.json", {"sources": sources, "posts_cache": posts_cache})
    return labels_path, sources_path


def test_run_excludes_unclear_and_never_touches_data_dir(tmp_path):
    labels_path, sources_path = _labels_and_sources(tmp_path)
    out_path = tmp_path / "out" / "probe.json"
    cache_path = tmp_path / "cache.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return '<div class="amazon-item-box"><a href="/dp/B0X" title="商品名">x</a></div>'

    report = run(
        sources_path=sources_path, labels_path=labels_path, out_path=out_path, cache_path=cache_path,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert out_path.exists()
    assert not (tmp_path / "data").exists()
    assert len(report["positive_asins"]) == 2
    assert len(report["negative_asins"]) == 1
    assert "B0UNCLEAR" not in report["positive_asins"] + report["negative_asins"]


def test_run_reuses_cache_on_second_call_without_refetching(tmp_path):
    labels_path, sources_path = _labels_and_sources(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return '<div class="amazon-item-box"><a href="/dp/B0X" title="商品名">x</a></div>'

    run(sources_path=sources_path, labels_path=labels_path, out_path=out_path, cache_path=cache_path,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object())
    assert len(calls) > 0

    def failing_fetch(post_id, wp_base_url, session, sleeper):
        raise AssertionError("キャッシュ済みの post を再取得しようとした")

    report = run(
        sources_path=sources_path, labels_path=labels_path, out_path=out_path, cache_path=cache_path,
        sleeper=lambda s: None, fetch_fn=failing_fetch, session=object(),
    )
    assert report["cache_miss_asins"] == []


def test_run_limit_caps_labeled_items(tmp_path):
    labels_path, sources_path = _labels_and_sources(tmp_path)
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return "<p>x</p>"

    report = run(
        sources_path=sources_path, labels_path=labels_path, out_path=out_path, cache_path=cache_path,
        limit=1, sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(calls) == 1
    assert report["total_pairs"] == 1


def test_run_reports_missing_post_id(tmp_path):
    labels_path, sources_path = _labels_and_sources(tmp_path)
    # posts_cache から B0NEG1 の post_url を消して引き当て失敗を作る
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    payload["posts_cache"] = {
        pid: entry for pid, entry in payload["posts_cache"].items()
        if entry["link"] != "https://omcha.jp/n1/"
    }
    sources_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out_path = tmp_path / "probe.json"
    cache_path = tmp_path / "cache.json"

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        return "<p>x</p>"

    report = run(
        sources_path=sources_path, labels_path=labels_path, out_path=out_path, cache_path=cache_path,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert report["missing_post_id_asins"] == ["B0NEG1"]
    assert "B0NEG1" not in report["negative_asins"]
