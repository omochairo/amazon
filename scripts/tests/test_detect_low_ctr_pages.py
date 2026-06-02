"""scripts/detect_low_ctr_pages.py unit tests (A-1, epic #1356)."""
from __future__ import annotations

import pytest

from scripts.detect_low_ctr_pages import (
    collect_top_queries_by_page,
    compute_baseline_ctr,
    detect,
)


def _make_gsc(by_page, by_combo=None, clicks_sum=100, impressions_sum=10000):
    return {
        "totals": {"clicks_sum": clicks_sum, "impressions_sum": impressions_sum},
        "by_page": by_page,
        "by_combo": by_combo or [],
        "range": {"start": "2026-05-26", "end": "2026-06-01"},
    }


def test_baseline_zero_impressions_returns_zero():
    assert compute_baseline_ctr({"clicks_sum": 0, "impressions_sum": 0}) == 0.0
    assert compute_baseline_ctr({}) == 0.0


def test_baseline_ratio():
    # 100 / 10000 = 0.01
    assert compute_baseline_ctr({"clicks_sum": 100, "impressions_sum": 10000}) == 0.01


def test_detect_excludes_low_impressions():
    # baseline = 0.01, threshold = max(0.005, 0.005) = 0.005
    # imp < 50 のページは impressions 不足で除外
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 30, "ctr": 0.0, "position": 5.0},
    ])
    result = detect(gsc)
    assert result["detected"] == []


def test_detect_excludes_page_2_results():
    # position > 10 のページは page 2 扱いで A-1 対象外 (A-2 で別途)
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 15.0},
    ])
    result = detect(gsc)
    assert result["detected"] == []


def test_detect_flags_low_ctr_above_threshold():
    # baseline = 100/10000 = 0.01, threshold = 0.005
    # imp >= 50, position <= 10, ctr < 0.005 ⇒ flag
    gsc = _make_gsc(by_page=[
        {"page": "/low/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 4.0},
        {"page": "/high/", "clicks": 50, "impressions": 500, "ctr": 0.10, "position": 4.0},
    ])
    result = detect(gsc)
    pages = [d["page"] for d in result["detected"]]
    assert pages == ["/low/"]


def test_detect_ratio_relative_to_baseline():
    # baseline = 1000/10000 = 0.10, ratio 0.5 → threshold = 0.05
    # ctr=0.04 (< 0.05) は flag、ctr=0.06 (>= 0.05) は通常
    gsc = _make_gsc(by_page=[
        {"page": "/below/", "clicks": 10, "impressions": 250, "ctr": 0.04, "position": 3.0},
        {"page": "/above/", "clicks": 15, "impressions": 250, "ctr": 0.06, "position": 3.0},
    ], clicks_sum=1000, impressions_sum=10000)
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/below/"]


def test_detect_abs_min_threshold_floor():
    # baseline 極小 (0.0001) でも abs_min_threshold (0.005) が下駄として効く
    # ctr=0.003 は abs_min より下なので flag
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 2, "impressions": 600, "ctr": 0.003, "position": 5.0},
    ], clicks_sum=1, impressions_sum=10000)
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/a/"]


def test_detect_sorts_by_impressions_desc():
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 100, "ctr": 0.0, "position": 5.0},
        {"page": "/b/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0},
        {"page": "/c/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 5.0},
    ])
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/b/", "/c/", "/a/"]


def test_detect_caps_at_max_results():
    gsc = _make_gsc(by_page=[
        {"page": f"/p{i}/", "clicks": 0, "impressions": 100 + i, "ctr": 0.0, "position": 5.0}
        for i in range(20)
    ])
    result = detect(gsc, max_results=3)
    assert len(result["detected"]) == 3


def test_detect_includes_top_queries_per_page():
    gsc = _make_gsc(
        by_page=[
            {"page": "/p/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0},
        ],
        by_combo=[
            {"page": "/p/", "query": "q1", "clicks": 0, "impressions": 300, "ctr": 0.0, "position": 5.0},
            {"page": "/p/", "query": "q2", "clicks": 0, "impressions": 100, "ctr": 0.0, "position": 7.0},
            {"page": "/other/", "query": "qx", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 3.0},
        ],
    )
    result = detect(gsc, top_queries_per_page=5)
    queries = result["detected"][0]["top_queries"]
    assert [q["query"] for q in queries] == ["q1", "q2"]


def test_detect_ratio_to_baseline_field():
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 1, "impressions": 500, "ctr": 0.002, "position": 5.0},
    ], clicks_sum=100, impressions_sum=10000)
    result = detect(gsc)
    assert result["detected"][0]["ratio_to_baseline"] == pytest.approx(0.002 / 0.01)


def test_collect_top_queries_handles_missing_page():
    grouped = collect_top_queries_by_page([
        {"query": "q1", "clicks": 0, "impressions": 100},
        {"page": "/a/", "query": "q2", "clicks": 0, "impressions": 200},
    ], n=5)
    assert list(grouped.keys()) == ["/a/"]
