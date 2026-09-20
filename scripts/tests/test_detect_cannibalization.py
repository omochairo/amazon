"""scripts/detect_cannibalization.py unit tests (A-3, epic #1356).

この検出器は 2026-09-01 まで unit test を持っていなかった。同日の閾値較正
(#5941 / amazon-navi-brain#18) で既定値を動かしたので、**較正が戻ったら気づける形**
にしておく。2026-09-20 (amazon-navi-brain#56) にサイトのトラフィックがさらに
減ったため再較正し、既定値をもう一段下げた (この回は min_page_impressions の
ほうが binding だった。#18 の回は min_query_impressions が binding で、
binding な側は固定ではなくサイトの実寸に追随して動く)。
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
    # #18 較正 (2026-09-01) は query 15 / page 5。#56 再較正 (2026-09-20) で
    # さらに下げた。この 2 つが戻ると下の 2 本が落ちる。
    assert DEFAULT_MIN_QUERY_IMPRESSIONS == 10
    assert DEFAULT_MIN_PAGE_IMPRESSIONS == 3


def test_query_volume_just_at_calibrated_threshold_is_detected():
    # 合計 10 = 較正後の下限ちょうど。#18 較正 (15) では拾えなかった帯。
    result = detect(_gsc([
        _combo("q", "/a/", 5),
        _combo("q", "/b/", 5),
    ]))
    assert [d["query"] for d in result["detected"]] == ["q"]


def test_query_volume_below_threshold_is_not_eligible():
    result = detect(_gsc([
        _combo("q", "/a/", 4),
        _combo("q", "/b/", 4),
    ]))
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_params_are_reported():
    result = detect(_gsc([]))
    assert result["params"]["min_query_impressions"] == DEFAULT_MIN_QUERY_IMPRESSIONS
    assert result["params"]["min_page_impressions"] == DEFAULT_MIN_PAGE_IMPRESSIONS
