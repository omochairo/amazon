"""#7569 型6 評価セット冊子ビルダーの検査。

ネットワークは一切叩かない (fetch_fn を注入)。HTML はすべて固定文字列。
"""
from __future__ import annotations

import json
import pathlib

from scripts.build_first_party_eval_packet import (
    CALIBRATION_ASINS,
    TITLE_LABELED_ASINS,
    build_item_order,
    classify_prior_label,
    first_link_offset_in_plain,
    index_sources_by_asin,
    load_b0_pool,
    pick_primary_post,
    pick_product_name,
    redact_title_occurrences,
    render_key,
    render_packet,
    run,
    sample_pool_asins,
    title_redaction_candidates,
    truncate_body,
)

# --------------------------------------------------------------------------
# 抽出母集団・順序
# --------------------------------------------------------------------------

def test_load_b0_pool_filters_non_b0_and_dedupes_sorted(tmp_path):
    p = tmp_path / "pool.json"
    p.write_text(
        json.dumps({"asins": ["B00D3UOBHC", "4057507701", "B00D3UOBHC", "B000EVMHIS"]}),
        encoding="utf-8",
    )
    assert load_b0_pool(p) == ["B000EVMHIS", "B00D3UOBHC"]


def test_sample_pool_asins_is_reproducible_with_same_seed():
    import random
    pool = [f"B0{i:08d}" for i in range(20)]
    a = sample_pool_asins(pool, 5, random.Random(42))
    b = sample_pool_asins(pool, 5, random.Random(42))
    assert a == b
    assert len(a) == 5


def test_sample_pool_asins_caps_at_population_size():
    import random
    pool = ["B0AAAAAAAA", "B0BBBBBBBB"]
    assert len(sample_pool_asins(pool, 40, random.Random(1))) == 2


def test_build_item_order_shuffles_reproducibly_with_same_seed():
    import random
    order_a = build_item_order(["B0X", "B0Y"], ("B0CAL1", "B0CAL2"), random.Random(7))
    order_b = build_item_order(["B0X", "B0Y"], ("B0CAL1", "B0CAL2"), random.Random(7))
    assert order_a == order_b
    assert set(order_a) == {"B0X", "B0Y", "B0CAL1", "B0CAL2"}


def test_classify_prior_label_calibration_positive():
    calibration_asin = next(iter(CALIBRATION_ASINS))
    assert classify_prior_label(calibration_asin) == "calibration_positive"


def test_classify_prior_label_title_labeled_ii():
    labeled_asin = next(iter(TITLE_LABELED_ASINS))
    assert classify_prior_label(labeled_asin) == "title_labeled_ii"


def test_classify_prior_label_unlabeled_for_unknown_asin():
    assert classify_prior_label("B0ZZZZZZZZ") == "unlabeled"


# --------------------------------------------------------------------------
# post の引き当て
# --------------------------------------------------------------------------

def test_pick_primary_post_returns_alphabetically_first_post_url_for_ties():
    sources_by_asin = {
        "B0X": [
            {"asin": "B0X", "role": "primary", "post_url": "https://omcha.jp/z/"},
            {"asin": "B0X", "role": "primary", "post_url": "https://omcha.jp/a/"},
            {"asin": "B0X", "role": "compared", "post_url": "https://omcha.jp/skip/"},
        ],
    }
    row = pick_primary_post(sources_by_asin, "B0X")
    assert row["post_url"] == "https://omcha.jp/a/"


def test_pick_primary_post_returns_none_when_no_primary_row():
    sources_by_asin = {"B0X": [{"asin": "B0X", "role": "compared", "post_url": "https://omcha.jp/a/"}]}
    assert pick_primary_post(sources_by_asin, "B0X") is None


def test_index_sources_by_asin_groups_rows():
    sources = [{"asin": "B0X", "role": "primary"}, {"asin": "B0X", "role": "compared"}, {"asin": "B0Y"}]
    idx = index_sources_by_asin(sources)
    assert len(idx["B0X"]) == 2
    assert len(idx["B0Y"]) == 1


# --------------------------------------------------------------------------
# post_title の伏せ字化
# --------------------------------------------------------------------------

def test_title_redaction_candidates_full_title_only():
    assert title_redaction_candidates("シンプルなタイトル") == ["シンプルなタイトル"]


def test_title_redaction_candidates_splits_on_full_width_pipe():
    candidates = title_redaction_candidates("よだれかけはいつまで？｜素材別の選び方")
    assert candidates[0] == "よだれかけはいつまで？｜素材別の選び方"
    assert "よだれかけはいつまで？" in candidates


def test_title_redaction_candidates_splits_on_brackets():
    candidates = title_redaction_candidates("【徹底比較】名曲ピアノえほん 新装版と改訂版の違いは？")
    assert "【徹底比較】" in candidates


def test_title_redaction_candidates_empty_title_returns_empty_list():
    assert title_redaction_candidates(None) == []
    assert title_redaction_candidates("") == []


def test_redact_title_occurrences_removes_full_title_and_counts():
    text = "これは【徹底比較】名曲ピアノえほんの本文です。名曲ピアノえほんが良いです。"
    candidates = title_redaction_candidates("【徹底比較】名曲ピアノえほん")
    redacted, count = redact_title_occurrences(text, candidates)
    assert "【徹底比較】名曲ピアノえほん" not in redacted
    assert count >= 1


def test_redact_title_occurrences_no_match_returns_zero_count():
    redacted, count = redact_title_occurrences("関係ない本文", ["別の題名"])
    assert redacted == "関係ない本文"
    assert count == 0


# --------------------------------------------------------------------------
# 本文の切り出し
# --------------------------------------------------------------------------

def test_first_link_offset_in_plain_finds_position():
    html = '<p>前置き</p><div class="amazon-item-box"><a href="/dp/B0AAAAAAAA">商品</a></div>'
    offset = first_link_offset_in_plain(html, "B0AAAAAAAA")
    assert offset is not None
    assert offset >= len("前置き")


def test_first_link_offset_in_plain_returns_none_when_not_found():
    html = '<p>この記事にはリンクが無い</p>'
    assert first_link_offset_in_plain(html, "B0AAAAAAAA") is None


def test_truncate_body_returns_unchanged_when_under_cap():
    text = "短い本文"
    body, truncated = truncate_body(text, 2)
    assert body == text
    assert truncated is False


def test_truncate_body_head_plus_window_with_ellipsis():
    full = "あ" * 1200 + "い" * 2000 + "う" * 2000
    link_offset = 3200  # 先頭ブロックから十分離れた位置
    body, truncated = truncate_body(full, link_offset)
    assert truncated is True
    assert "…（中略）…" in body
    assert body.startswith("あ" * 1200)
    assert len(body) <= 1200 + len("…（中略）…") + 1600


def test_truncate_body_merges_when_window_overlaps_head():
    full = "あ" * 3000
    body, truncated = truncate_body(full, 1300)  # window_start(500) <= head(1200)
    assert truncated is False
    assert "…（中略）…" not in body


def test_truncate_body_falls_back_to_head_cut_when_link_offset_missing():
    full = "あ" * 3000
    body, truncated = truncate_body(full, None)
    assert len(body) == 2000
    assert truncated is False


def test_pick_product_name_returns_first_candidate_or_none():
    html = (
        '<div class="amazon-item-box">'
        '<a class="amazon-item-title-link" href="/dp/B0AAAAAAAA" title="テスト商品">テスト商品</a>'
        '</div>'
    )
    assert pick_product_name(html, "B0AAAAAAAA") == "テスト商品"
    assert pick_product_name("<p>商品ボックスが無い</p>", "B0AAAAAAAA") is None


# --------------------------------------------------------------------------
# 冊子・キーの組み立て
# --------------------------------------------------------------------------

def test_render_packet_includes_judgement_instructions_and_items():
    items = [
        {"item_id": "item-01", "product_name": "商品A", "body_text": "本文A", "redaction_count": 0},
        {"item_id": "item-02", "product_name": None, "body_text": "本文B", "redaction_count": 2},
    ]
    md = render_packet(items)
    assert "review" in md and "mention" in md and "unclear" in md
    assert "## item-01" in md and "## item-02" in md
    assert "(商品名候補なし)" in md
    assert "**判定**: " in md
    assert "合計 2 箇所" in md


def test_render_key_hides_no_post_title_field_name_conflict_and_records_seed():
    items = [
        {
            "item_id": "item-01", "asin": "B0X", "post_url": "https://omcha.jp/x/",
            "post_title": "タイトルX", "prior_title_label": "unlabeled", "redaction_count": 1,
        },
    ]
    key = render_key(items, seed=7569, sample_size=40, pool_total=159)
    assert key["seed"] == 7569
    assert key["sample_size"] == 40
    assert key["pool_total"] == 159
    assert key["post_title_redaction_total_occurrences"] == 1
    assert key["items"]["item-01"]["asin"] == "B0X"
    assert key["items"]["item-01"]["prior_title_label"] == "unlabeled"


# --------------------------------------------------------------------------
# run (統合。fetch_fn を注入しネットワークを叩かない)
# --------------------------------------------------------------------------

def _write_json(path: pathlib.Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _setup_fixture(tmp_path: pathlib.Path, pool_asins: list[str]) -> tuple[pathlib.Path, pathlib.Path]:
    all_asins = list(pool_asins) + list(CALIBRATION_ASINS)
    sources = [
        {
            "asin": a, "role": "primary", "role_reason": "mention_share",
            "post_url": f"https://omcha.jp/{a.lower()}/",
            "post_title": f"【テスト】{a} のタイトル｜サブ見出し",
            "has_article": a in CALIBRATION_ASINS,
        }
        for a in all_asins
    ]
    posts_cache = {
        str(200 + i): {"link": f"https://omcha.jp/{a.lower()}/"}
        for i, a in enumerate(all_asins)
    }
    sources_path = tmp_path / "sources.json"
    pool_path = tmp_path / "pool.json"
    _write_json(sources_path, {"sources": sources, "posts_cache": posts_cache})
    _write_json(pool_path, {"asins": pool_asins})
    return sources_path, pool_path


def _fake_fetch(post_id, wp_base_url, session, sleeper):
    return (
        '<p>本文の書き出し。</p>'
        '<div class="amazon-item-box">'
        '<a class="amazon-item-title-link" href="/dp/B0AAAAAAAA" title="ダミー商品">ダミー商品</a>'
        '</div>'
        '<p>本文の続き。</p>'
    )


def test_run_writes_packet_and_key_without_touching_data_dir(tmp_path):
    pool_asins = [f"B0{i:08d}" for i in range(10)]
    sources_path, pool_path = _setup_fixture(tmp_path, pool_asins)
    out_packet = tmp_path / "out" / "packet.md"
    out_key = tmp_path / "out" / "key.json"
    cache_path = tmp_path / "cache.json"

    result = run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=5, limit=0,
        sleeper=lambda s: None, fetch_fn=_fake_fetch, session=object(),
    )
    assert out_packet.exists()
    assert out_key.exists()
    assert not (tmp_path / "data").exists()
    assert len(result["items"]) == 5 + len(CALIBRATION_ASINS)


def test_run_is_reproducible_with_same_seed(tmp_path):
    pool_asins = [f"B0{i:08d}" for i in range(15)]
    sources_path, pool_path = _setup_fixture(tmp_path, pool_asins)

    def run_once(tag):
        out_packet = tmp_path / f"packet_{tag}.md"
        out_key = tmp_path / f"key_{tag}.json"
        cache_path = tmp_path / f"cache_{tag}.json"
        return run(
            sources_path=sources_path, pool_path=pool_path,
            out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
            seed=99, sample_size=5, limit=0,
            sleeper=lambda s: None, fetch_fn=_fake_fetch, session=object(),
        )

    result_a = run_once("a")
    result_b = run_once("b")
    order_a = [i["asin"] for i in result_a["items"]]
    order_b = [i["asin"] for i in result_b["items"]]
    assert order_a == order_b


def test_run_reuses_cache_on_second_call_without_refetching(tmp_path):
    pool_asins = [f"B0{i:08d}" for i in range(5)]
    sources_path, pool_path = _setup_fixture(tmp_path, pool_asins)
    out_packet = tmp_path / "packet.md"
    out_key = tmp_path / "key.json"
    cache_path = tmp_path / "cache.json"
    calls = []

    def fake_fetch(post_id, wp_base_url, session, sleeper):
        calls.append(post_id)
        return _fake_fetch(post_id, wp_base_url, session, sleeper)

    run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=5, limit=0,
        sleeper=lambda s: None, fetch_fn=fake_fetch, session=object(),
    )
    assert len(calls) > 0

    def failing_fetch(post_id, wp_base_url, session, sleeper):
        raise AssertionError("キャッシュ済みの post を再取得しようとした")

    run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=5, limit=0,
        sleeper=lambda s: None, fetch_fn=failing_fetch, session=object(),
    )


def test_run_limit_caps_item_count(tmp_path):
    pool_asins = [f"B0{i:08d}" for i in range(20)]
    sources_path, pool_path = _setup_fixture(tmp_path, pool_asins)
    out_packet = tmp_path / "packet.md"
    out_key = tmp_path / "key.json"
    cache_path = tmp_path / "cache.json"

    result = run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=10, limit=3,
        sleeper=lambda s: None, fetch_fn=_fake_fetch, session=object(),
    )
    assert len(result["items"]) == 3


def test_run_redacts_post_title_from_body_text(tmp_path):
    pool_asins = ["B0AAAAAAAA"]
    all_asins = pool_asins + list(CALIBRATION_ASINS)
    sources = [
        {
            "asin": a, "role": "primary", "role_reason": "mention_share",
            "post_url": f"https://omcha.jp/{a.lower()}/",
            "post_title": "ダミー商品の徹底レビュー",
            "has_article": a in CALIBRATION_ASINS,
        }
        for a in all_asins
    ]
    posts_cache = {
        str(300 + i): {"link": f"https://omcha.jp/{a.lower()}/"}
        for i, a in enumerate(all_asins)
    }
    sources_path = tmp_path / "sources.json"
    pool_path = tmp_path / "pool.json"
    _write_json(sources_path, {"sources": sources, "posts_cache": posts_cache})
    _write_json(pool_path, {"asins": pool_asins})
    out_packet = tmp_path / "packet.md"
    out_key = tmp_path / "key.json"
    cache_path = tmp_path / "cache.json"

    def fetch_with_title_leak(post_id, wp_base_url, session, sleeper):
        return (
            '<p>ダミー商品の徹底レビューをお届けします。</p>'
            '<div class="amazon-item-box">'
            '<a class="amazon-item-title-link" href="/dp/B0AAAAAAAA" title="ダミー商品">ダミー商品</a>'
            '</div>'
        )

    result = run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=1, limit=0,
        sleeper=lambda s: None, fetch_fn=fetch_with_title_leak, session=object(),
    )
    target_item = next(i for i in result["items"] if i["asin"] == "B0AAAAAAAA")
    assert "ダミー商品の徹底レビュー" not in target_item["body_text"]
    assert target_item["redaction_count"] >= 1
    packet_text = out_packet.read_text(encoding="utf-8")
    assert "ダミー商品の徹底レビュー" not in packet_text
    key_data = json.loads(out_key.read_text(encoding="utf-8"))
    assert key_data["post_title_redaction_total_occurrences"] >= 1


def test_run_marks_missing_asin_when_no_primary_post(tmp_path):
    pool_asins = ["B0AAAAAAAA"]
    sources_path = tmp_path / "sources.json"
    pool_path = tmp_path / "pool.json"
    all_asins = pool_asins + list(CALIBRATION_ASINS)
    sources = [
        {"asin": a, "role": "compared", "post_url": f"https://omcha.jp/{a.lower()}/", "post_title": "x"}
        for a in all_asins
    ]
    _write_json(sources_path, {"sources": sources, "posts_cache": {}})
    _write_json(pool_path, {"asins": pool_asins})
    out_packet = tmp_path / "packet.md"
    out_key = tmp_path / "key.json"
    cache_path = tmp_path / "cache.json"

    result = run(
        sources_path=sources_path, pool_path=pool_path,
        out_packet_path=out_packet, out_key_path=out_key, cache_path=cache_path,
        seed=1, sample_size=1, limit=0,
        sleeper=lambda s: None, fetch_fn=_fake_fetch, session=object(),
    )
    assert set(result["missing_asins"]) == set(all_asins)
    assert all(i["body_text"].startswith("(本文を取得できなかった") for i in result["items"])
