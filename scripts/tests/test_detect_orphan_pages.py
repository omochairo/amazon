"""scripts/detect_orphan_pages.py unit tests (A-5, epic #1356).

この検出器は 2026-09-20 (amazon-navi-brain#56) まで unit test を持っていなかった。
同日 min_pv=50→5 への引き下げを一度提案したが、レビューで「GA4/GSC の乖離が
未確認」「min_pv=5 では entrance_ratio が二値に縮退する」「既知の真陽性を
確認していない」と指摘され保留になった (detect_orphan_pages.py のコメント参照)。
閾値は現行の 50 のまま、**較正が入ったら気づける形**だけ先に整備しておく。
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
    result = detect(_ga4([_page("/products/a/", DEFAULT_MIN_PV, DEFAULT_MIN_PV)]))
    assert [d["page_path"] for d in result["detected"]] == ["/products/a/"]
    assert result["eligible"] == 1


def test_low_entrance_ratio_is_not_detected_but_counts_as_eligible():
    # 内部流入があるページは孤児ではない。母数には数える。
    result = detect(_ga4([_page("/products/a/", DEFAULT_MIN_PV, DEFAULT_MIN_PV // 3)]))
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


# --- 既定値が動いたら気づけるようにしておく (#56 のレビューで保留中) ------

def test_default_min_pv_is_still_the_pre_56_value():
    # #56 で 50→5 を提案したがレビューで保留 (detect_orphan_pages.py のコメント
    # 参照)。下げるなら GA4/GSC 乖離の解消 + entrance_ratio の丸め粒度 + 既知の
    # 真陽性確認の 3 点を先にやってからにすること。
    assert DEFAULT_MIN_PV == 50
    assert DEFAULT_MIN_ENTRANCE_RATIO == 0.90


def test_pv_just_at_threshold_is_eligible():
    result = detect(_ga4([_page("/products/a/", DEFAULT_MIN_PV, DEFAULT_MIN_PV)]))
    assert result["eligible"] == 1


def test_params_are_reported():
    result = detect(_ga4([]))
    assert result["params"]["min_pv"] == DEFAULT_MIN_PV
    assert result["params"]["min_entrance_ratio"] == DEFAULT_MIN_ENTRANCE_RATIO
