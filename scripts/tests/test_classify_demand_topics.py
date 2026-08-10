"""需要クエリのトピック分類 (#2686/#3332 N2) の検査。"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import classify_demand_topics as CT  # noqa: E402

REAL_TERMS = ROOT.parent / "data" / "demand_topic_terms.yaml"


def _write_terms(tmp_path: pathlib.Path, buckets: dict) -> pathlib.Path:
    p = tmp_path / "terms.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "buckets": buckets}, allow_unicode=True),
                 encoding="utf-8")
    return p


def _terms(tmp_path, **buckets):
    return CT.load_terms(_write_terms(tmp_path, buckets))


# --------------------------------------------------------------------------
# 語彙の読み込み
# --------------------------------------------------------------------------

def test_unknown_bucket_is_ignored_not_silently_merged(tmp_path, caplog):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]}, typo_bucket={"terms": ["誤記"]})
    assert set(terms) == set(CT.BUCKET_ORDER)
    assert "typo_bucket" in caplog.text


def test_missing_bucket_becomes_empty_vocabulary(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]})
    assert terms["parenting"]["terms"] == []


def test_terms_are_normalized_on_load(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["ＴＯＭＩＣＡ"]})  # 全角
    assert terms["toy"]["terms"] == ["tomica"]


# --------------------------------------------------------------------------
# 分類
# --------------------------------------------------------------------------

def test_out_of_scope_wins_over_toy(tmp_path):
    """mbti 等は玩具語と同居しても対象外 (先勝ちの順序を固定する)。"""
    terms = _terms(tmp_path, toy={"terms": ["おもちゃ"]}, out_of_scope={"terms": ["mbti"]})
    assert CT.classify("mbti診断 子供向け おもちゃ", terms)[0] == "out_of_scope"


def test_toy_wins_over_baby_goods(tmp_path):
    """「赤ちゃん ハンドスピナー」は玩具側へ倒す (navi のコアは玩具)。"""
    terms = _terms(tmp_path, toy={"terms": ["ハンドスピナー"]}, baby_goods={"terms": ["赤ちゃん"]})
    assert CT.classify("赤ちゃん ハンドスピナー おすすめ", terms)[0] == "toy"


def test_longest_matched_term_is_reported_as_evidence(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ", "トミカ収納"]})
    assert CT.classify("トミカ収納 100均", terms)[1] == "トミカ収納"


def test_query_is_normalized_before_matching(tmp_path):
    terms = _terms(tmp_path, out_of_scope={"terms": ["mbti"]})
    assert CT.classify("ＭＢＴＩ　診断", terms)[0] == "out_of_scope"


def test_unknown_query_falls_to_unclassified_not_a_bucket(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]})
    bucket, matched = CT.classify("まったく未知の語", terms)
    assert bucket == CT.UNCLASSIFIED
    assert matched is None


# --------------------------------------------------------------------------
# 実語彙の回帰 (2026-08-10 に実データで踏んだ欠陥)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_terms():
    if not REAL_TERMS.exists():  # pragma: no cover
        pytest.skip("terms yaml not present")
    return CT.load_terms(REAL_TERMS)


@pytest.mark.parametrize("query", [
    "エンジェルサウンズ 後悔",
    "ネオママイズム いつまで",
    "イーピコ 口コミ",
])
def test_unknown_product_with_suffix_stays_unclassified(real_terms, query):
    """修飾語 (後悔/いつまで/口コミ) をトピック語彙に入れないこと。

    入れると語彙に無い商品名が parenting に吸い込まれ、**未知商品が可視化され
    なくなる**。未分類のまま残すのが正しい (未知を pass に潰さない)。
    """
    assert CT.classify(query, real_terms)[0] == CT.UNCLASSIFIED


@pytest.mark.parametrize("query,expected", [
    ("メロジョイ 偽物", "toy"),          # スクイーズ玩具ブランド (WP 最大クラスタ)
    ("メロンジョイ偽物見分け方", "toy"),   # 表記ゆれ
    ("トミカ 収納", "toy"),
    ("スクイーズ どこで 買える", "toy"),
    ("授乳クッション おすすめ", "baby_goods"),
    ("粉ミルク 比較", "baby_goods"),
    ("64タイプ診断", "out_of_scope"),
    ("mbti 64", "out_of_scope"),
    ("一升餅 シャトレーゼ", "out_of_scope"),
    ("赤ちゃん 泣き声 聞き分け", "parenting"),
])
def test_real_vocabulary_classifies_known_clusters(real_terms, query, expected):
    assert CT.classify(query, real_terms)[0] == expected


def test_no_generic_suffix_terms_in_vocabulary(real_terms):
    """修飾語が語彙に混ざっていないことを構造で守る。"""
    forbidden = {"後悔", "いらない", "いつから", "いつまで", "何歳から", "何歳まで",
                 "知恵袋", "口コミ", "おすすめ", "ランキング", "体に悪い", "危ない"}
    for name, spec in real_terms.items():
        overlap = forbidden & set(spec["terms"])
        assert not overlap, f"{name} に修飾語が混入: {overlap}"


# --------------------------------------------------------------------------
# レポート
# --------------------------------------------------------------------------

def test_build_report_counts_queries_and_impressions_per_bucket(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]}, out_of_scope={"terms": ["mbti"]})
    demand = [
        {"query": "トミカ 収納", "wp_impressions": 100, "impressions": 0},
        {"query": "トミカ ケース", "wp_impressions": 50, "impressions": 2},
        {"query": "mbti 64", "wp_impressions": 30, "impressions": 0},
        {"query": "未知の語", "wp_impressions": 10, "impressions": 0},
    ]
    rep = CT.build_report(demand, terms, top_unclassified=5)
    assert rep["summary"]["toy"]["queries"] == 2
    assert rep["summary"]["toy"]["wp_impressions"] == 150
    assert rep["summary"]["out_of_scope"]["queries"] == 1
    assert rep["summary"][CT.UNCLASSIFIED]["queries"] == 1
    assert rep["total_queries"] == 4
    assert rep["total_wp_impressions"] == 190


def test_unclassified_top_is_impression_ordered_and_capped(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]})
    demand = [{"query": f"未知{i}", "wp_impressions": i, "impressions": 0} for i in range(10)]
    rep = CT.build_report(demand, terms, top_unclassified=3)
    assert [r["wp_impressions"] for r in rep["unclassified_top"]] == [9, 8, 7]


def test_rows_are_impression_ordered(tmp_path):
    terms = _terms(tmp_path, toy={"terms": ["トミカ"]})
    demand = [
        {"query": "トミカ a", "wp_impressions": 5, "impressions": 0},
        {"query": "トミカ b", "wp_impressions": 99, "impressions": 0},
    ]
    rep = CT.build_report(demand, terms, top_unclassified=5)
    assert [r["wp_impressions"] for r in rep["rows"]] == [99, 5]
