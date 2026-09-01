"""scripts/detect_cannibalization.py unit tests (A-3, epic #1356).

この検出器は 2026-09-01 まで unit test を持っていなかった。同日の閾値較正
(#5941 / amazon-navi-brain#18) で既定値を動かしたので、**較正が戻ったら気づける形**
にしておく。スイープで binding だったのは min_query_impressions のほうで、
min_page_impressions をどれだけ下げても旧 min_query では 0 のままだった。
"""
from __future__ import annotations

from scripts.detect_cannibalization import (
    DEFAULT_MIN_PAGE_IMPRESSIONS,
    DEFAULT_MIN_QUERY_IMPRESSIONS,
    detect,
)


def _combo(query, page, impressions, position=10.0, clicks=0):
    return {
        "query": query, "page": page, "clicks": clicks,
        "impressions": impressions, "ctr": 0.0, "position": position,
    }


def _gsc(rows):
    return {"by_combo": rows, "range": {"start": "2026-08-25", "end": "2026-08-31"}}


def test_detects_two_pages_competing_for_one_query():
    result = detect(_gsc([
        _combo("ロンビー", "/a/", 10),
        _combo("ロンビー", "/b/", 8),
    ]))
    assert [d["query"] for d in result["detected"]] == ["ロンビー"]
    assert result["detected"][0]["competing_page_count"] == 2
    assert result["detected"][0]["total_impressions"] == 18


def test_single_page_is_not_cannibalization():
    result = detect(_gsc([_combo("ロンビー", "/a/", 50)]))
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_dominant_page_is_excluded():
    # 1 ページが圧倒的なら実質カニバっていない。母数には数える。
    result = detect(_gsc([
        _combo("ロンビー", "/a/", 100),
        _combo("ロンビー", "/b/", 5),
    ]))
    assert result["detected"] == []
    assert result["eligible"] == 1, "支配率で落ちたクエリも母数には数える"


def test_pages_below_page_threshold_do_not_count_as_competing():
    below = DEFAULT_MIN_PAGE_IMPRESSIONS - 1
    result = detect(_gsc([
        _combo("q", "/a/", below),
        _combo("q", "/b/", below),
    ]))
    assert result["detected"] == []


# --- 較正が戻ったら落ちるテスト -------------------------------------------

def test_calibrated_defaults():
    # 較正前は query 50 / page 10 だった。この 2 つが戻ると下の 2 本が落ちる。
    assert DEFAULT_MIN_QUERY_IMPRESSIONS == 15
    assert DEFAULT_MIN_PAGE_IMPRESSIONS == 5


def test_query_volume_just_at_calibrated_threshold_is_detected():
    # 合計 15 = 較正後の下限ちょうど。較正前 (50) では拾えなかった帯。
    result = detect(_gsc([
        _combo("q", "/a/", 8),
        _combo("q", "/b/", 7),
    ]))
    assert [d["query"] for d in result["detected"]] == ["q"]


def test_query_volume_below_threshold_is_not_eligible():
    result = detect(_gsc([
        _combo("q", "/a/", 7),
        _combo("q", "/b/", 7),
    ]))
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_params_are_reported():
    result = detect(_gsc([]))
    assert result["params"]["min_query_impressions"] == DEFAULT_MIN_QUERY_IMPRESSIONS
    assert result["params"]["min_page_impressions"] == DEFAULT_MIN_PAGE_IMPRESSIONS
