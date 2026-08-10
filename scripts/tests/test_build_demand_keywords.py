"""需要クエリ → Amazon 検索キーワード変換 (#2686) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_demand_keywords as B  # noqa: E402

REAL_TERMS = ROOT.parent / "data" / "demand_topic_terms.yaml"


def _terms_file(tmp_path, modifiers=None, excluded=None):
    p = tmp_path / "terms.yaml"
    p.write_text(yaml.safe_dump(
        {"version": 1, "search_modifiers": modifiers or [], "excluded_keywords": excluded or []},
        allow_unicode=True), encoding="utf-8")
    return p


def _mods(tmp_path, modifiers):
    return B.load_vocab(_terms_file(tmp_path, modifiers=modifiers))[0]


# --------------------------------------------------------------------------
# 正規化 (空白除去)
# --------------------------------------------------------------------------

def test_normalize_key_removes_all_spaces():
    """GSC の分かち書き揺れを吸収する。"""
    assert B.normalize_key("はじめて ず かん 1000") == "はじめてずかん1000"
    assert B.normalize_key("トミカ　収納") == "トミカ収納"


def test_spacing_variants_collapse_to_one_keyword(tmp_path):
    """「トミカ 収納」と「トミカ収納」が別集計にならないこと (実データで踏んだ)。"""
    mods = _mods(tmp_path, ["収納ケース"])  # 収納 は商品カテゴリなので落とさない
    assert B.to_search_keyword("トミカ 収納", mods) == B.to_search_keyword("トミカ収納", mods)


def test_modifier_split_by_spaces_is_still_stripped(tmp_path):
    """「危険 性」のように分断された修飾語も落ちること (実データで踏んだ)。"""
    mods = _mods(tmp_path, ["危険性"])
    assert B.to_search_keyword("ジップストリング 危険 性", mods) == "ジップストリング"


# --------------------------------------------------------------------------
# 修飾語の除去
# --------------------------------------------------------------------------

def test_longer_modifier_is_stripped_before_shorter(tmp_path):
    """「どこで売ってる」を先に落とさないと「どこで」が残る。"""
    mods = _mods(tmp_path, ["売ってる", "どこで売ってる"])
    assert B.to_search_keyword("スクイーズ どこで売ってる", mods) == "スクイーズ"


def test_trailing_particle_residue_is_cleaned(tmp_path):
    mods = _mods(tmp_path, ["違い"])
    assert B.to_search_keyword("はじめてずかん1000と1500の違い", mods) == "はじめてずかん1000と1500"


def test_keyword_shorter_than_minimum_is_dropped(tmp_path):
    mods = _mods(tmp_path, ["おすすめ"])
    assert B.to_search_keyword("おすすめ", mods) == ""


def test_product_category_word_is_not_treated_as_modifier():
    """「収納」を落とすとミニカー本体が返り検索意図が変わる。実語彙で守る。"""
    mods, _ = B.load_vocab(REAL_TERMS)
    assert "収納" not in mods
    assert B.to_search_keyword("トミカ 収納 100均 ダイソー", mods) == "トミカ収納"


# --------------------------------------------------------------------------
# 供給ゲート (Amazon に商品が無いもの)
# --------------------------------------------------------------------------

def test_excluded_keyword_is_skipped_with_reason(tmp_path):
    mods, excluded = B.load_vocab(_terms_file(
        tmp_path,
        modifiers=["偽物"],
        excluded=[{"keyword": "メロジョイ", "aliases": ["mellojoy"], "reason": "Amazon 非販売"}],
    ))
    topics = {"rows": [
        {"query": "メロジョイ 偽物", "wp_impressions": 100, "bucket": "toy"},
        {"query": "mellojoy どこの国", "wp_impressions": 50, "bucket": "toy"},
        {"query": "スクイーズ", "wp_impressions": 10, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, B.build_excluded_terms(excluded), ("toy",))
    assert [k["keyword"] for k in rep["keywords"]] == ["スクイーズ"]
    assert rep["summary"]["excluded_wp_impressions"] == 150
    assert all(e["reason"] for e in rep["excluded"]), "除外は理由つきで記録すること"


def test_alias_of_excluded_keyword_is_matched_after_space_removal(tmp_path):
    mods, excluded = B.load_vocab(_terms_file(
        tmp_path, excluded=[{"keyword": "メロジョイ", "aliases": ["メロン ジョイ"], "reason": "x"}]))
    terms = B.build_excluded_terms(excluded)
    assert "メロンジョイ" in terms


def test_real_vocabulary_excludes_amazon_unavailable_items():
    """owner 確認済みの非販売品・実店舗名が検索語に出ないこと。"""
    mods, excluded = B.load_vocab(REAL_TERMS)
    terms = B.build_excluded_terms(excluded)
    topics = {"rows": [
        {"query": "メロジョイ 偽物", "wp_impressions": 58768, "bucket": "toy"},
        {"query": "おもちゃのバンバン", "wp_impressions": 11300, "bucket": "toy"},
        {"query": "おもちゃ屋さんの倉庫 閉店", "wp_impressions": 917, "bucket": "toy"},
        {"query": "スクイーズ どこで 買える", "wp_impressions": 12010, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, terms, ("toy",))
    assert [k["keyword"] for k in rep["keywords"]] == ["スクイーズ"]


# --------------------------------------------------------------------------
# 集約
# --------------------------------------------------------------------------

def test_impressions_are_summed_per_keyword_with_source_queries(tmp_path):
    mods = _mods(tmp_path, ["一覧", "種類"])
    topics = {"rows": [
        {"query": "ナーフ 一覧", "wp_impressions": 100, "bucket": "toy"},
        {"query": "ナーフ 種類", "wp_impressions": 60, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, {}, ("toy",))
    assert len(rep["keywords"]) == 1
    k = rep["keywords"][0]
    assert k["keyword"] == "ナーフ" and k["wp_impressions"] == 160
    assert len(k["source_queries"]) == 2


def test_only_requested_buckets_are_used(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [
        {"query": "トミカ", "wp_impressions": 10, "bucket": "toy"},
        {"query": "授乳クッション", "wp_impressions": 999, "bucket": "baby_goods"},
    ]}
    assert [k["keyword"] for k in B.build(topics, mods, {}, ("toy",))["keywords"]] == ["トミカ"]
    both = B.build(topics, mods, {}, ("toy", "baby_goods"))["keywords"]
    assert len(both) == 2


def test_keywords_are_impression_ordered_and_limited(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [
        {"query": f"語{i}", "wp_impressions": i, "bucket": "toy"} for i in range(5)
    ]}
    rep = B.build(topics, mods, {}, ("toy",), limit=2)
    assert [k["wp_impressions"] for k in rep["keywords"]] == [4, 3]


def test_summary_counts_only_kept_keywords(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [{"query": f"語{i}", "wp_impressions": 10, "bucket": "toy"} for i in range(4)]}
    rep = B.build(topics, mods, {}, ("toy",), limit=2)
    assert rep["summary"]["keywords"] == 2
    assert rep["summary"]["wp_impressions"] == 20


@pytest.mark.parametrize("query,expected", [
    ("スクイーズ どこに売ってる", "スクイーズ"),
    ("ダイヤブロック 生産終了 なぜ", "ダイヤブロック"),
    ("ボーネルンド ルーピング販売終了 理由", "ボーネルンドルーピング"),
    ("ミッケ 難易度 ランキング", "ミッケ"),
    ("こえだちゃん 販売休止 理由", "こえだちゃん"),
])
def test_real_vocabulary_end_to_end(query, expected):
    mods, _ = B.load_vocab(REAL_TERMS)
    assert B.to_search_keyword(query, mods) == expected


# --------------------------------------------------------------------------
# 供給 probe による情報クエリの排除 (owner 指示 2026-08-10)
# --------------------------------------------------------------------------

def _probe_file(tmp_path, results):
    p = tmp_path / "probe.json"
    p.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")
    return p


def test_zero_hit_keywords_are_dropped_with_their_demand_recorded(tmp_path):
    """商品が 1 件も返らない語 = 商品名として通らない情報クエリ。落とす。"""
    probe = _probe_file(tmp_path, [
        {"keyword": "食玩法律", "hits": 0, "error": None},
        {"keyword": "トミカ収納", "hits": 10, "error": None},
    ])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [
        {"query": "食玩 法律", "wp_impressions": 192, "bucket": "toy"},
        {"query": "トミカ 収納", "wp_impressions": 21365, "bucket": "toy"},
    ]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert [k["keyword"] for k in rep["keywords"]] == ["トミカ収納"]
    assert rep["summary"]["dropped_no_supply"] == 1
    assert rep["summary"]["dropped_no_supply_wp_impressions"] == 192
    assert rep["dropped_no_supply"][0]["keyword"] == "食玩法律"


def test_api_error_keywords_are_not_dropped(tmp_path):
    """測れていないだけの語を「供給なし」と混同しない。"""
    probe = _probe_file(tmp_path, [{"keyword": "トミカ収納", "hits": 0, "error": "boom"}])
    assert B.load_zero_supply_keywords(probe) == set()


def test_keywords_absent_from_probe_are_kept(tmp_path):
    """新しい需要は未測定なので通す (probe に無い = 落とす、にしない)。"""
    probe = _probe_file(tmp_path, [{"keyword": "旧い語", "hits": 0, "error": None}])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [{"query": "新しい商品名", "wp_impressions": 100, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert [k["keyword"] for k in rep["keywords"]] == ["新しい商品名"]


def test_missing_or_broken_probe_disables_the_filter(tmp_path):
    assert B.load_zero_supply_keywords(None) == set()
    assert B.load_zero_supply_keywords(tmp_path / "nope.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert B.load_zero_supply_keywords(bad) == set()


def test_probe_keyword_matching_ignores_spacing(tmp_path):
    """probe 側と build 側で分かち書きが違っても同じ語として扱う。"""
    probe = _probe_file(tmp_path, [{"keyword": "食玩 法律", "hits": 0, "error": None}])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [{"query": "食玩法律", "wp_impressions": 10, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert rep["keywords"] == []


def test_real_probe_drops_only_informational_residue():
    """実 probe を当てて、落ちるのが情報クエリだけであることを固定する。"""
    probe = ROOT.parent / "data" / "analytics" / "demand_supply_probe.json"
    if not probe.exists():
        pytest.skip("probe report not present")
    zero = B.load_zero_supply_keywords(probe)
    # 商品名の語が巻き込まれていないこと
    for kw in ["トミカ収納", "スクイーズ", "ジョブレイバー", "知育ボックス", "ナーフ"]:
        assert B.normalize_key(kw) not in zero, f"商品名 {kw} が供給なし扱いになっている"
    # 情報クエリ残骸が落ちていること
    assert B.normalize_key("食玩法律") in zero
