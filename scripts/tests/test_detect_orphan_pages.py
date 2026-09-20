"""scripts/detect_orphan_pages.py unit tests (A-5, epic #1356).

この検出器は 2026-09-20 (amazon-navi-brain#56) まで unit test を持っていなかった。
同日、navi のトラフィック縮小で min_pv=50 では母数が枯れる週が続くことが分かり
既定値を下げたので、**較正が戻ったら気づける形**にしておく (#18 で A-3 に
unit test を新設したのと同じ理由)。
"""
from __future__ import annotations

from scripts.detect_orphan_pages import (
    DEFAULT_MIN_ENTRANCE_RATIO,
    DEFAULT_MIN_PV,
    detect,
)


def _page(path, pv, entrances, host="navi.omcha.jp"):
    return {
        "hostName": host, "pagePath": path,
        "screenPageViews": pv, "entrances": entrances,
    }


def _ga4(rows):
    return {"by_page": rows, "range": {"start": "2026-09-13", "end": "2026-09-20"}}


def test_detects_high_entrance_ratio_page():
    result = detect(_ga4([_page("/products/a/", 10, 10)]))
    assert [d["page_path"] for d in result["detected"]] == ["/products/a/"]
    assert result["eligible"] == 1


def test_low_entrance_ratio_is_not_detected_but_counts_as_eligible():
    # 内部流入があるページは孤児ではない。母数には数える。
    result = detect(_ga4([_page("/products/a/", 10, 3)]))
    assert result["detected"] == []
    assert result["eligible"] == 1


def test_below_pv_threshold_is_not_eligible():
    below = DEFAULT_MIN_PV - 1
    result = detect(_ga4([_page("/products/a/", below, below)]))
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_non_content_path_is_ignored():
    result = detect(_ga4([_page("/", 1000, 1000)]))
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_other_host_is_ignored():
    result = detect(_ga4([_page("/products/a/", 1000, 1000, host="omcha.jp")]))
    assert result["eligible"] == 0


def test_missing_entrances_is_skipped_not_zero():
    # entrances が無い行 (旧 artifact 等) は eligible に数えない (誤検出回避)。
    result = detect(_ga4([{
        "hostName": "navi.omcha.jp", "pagePath": "/products/a/",
        "screenPageViews": 1000,
    }]))
    assert result["eligible"] == 0
    assert result["detected"] == []


# --- 較正が戻ったら落ちるテスト -------------------------------------------

def test_calibrated_defaults():
    # 較正前 (#5941 / brain#18 導入時) は min_pv=50 だった。
    assert DEFAULT_MIN_PV == 5
    assert DEFAULT_MIN_ENTRANCE_RATIO == 0.90


def test_pv_just_at_calibrated_threshold_is_eligible():
    # 較正後の下限ちょうど。旧値 (50) では拾えなかった帯。
    result = detect(_ga4([_page("/products/a/", DEFAULT_MIN_PV, DEFAULT_MIN_PV)]))
    assert result["eligible"] == 1


def test_params_are_reported():
    result = detect(_ga4([]))
    assert result["params"]["min_pv"] == DEFAULT_MIN_PV
    assert result["params"]["min_entrance_ratio"] == DEFAULT_MIN_ENTRANCE_RATIO
