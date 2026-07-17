"""scripts/detect_article_feedback_worst_pages.py unit tests (issue #2051)."""
from __future__ import annotations

from scripts.detect_article_feedback_worst_pages import (
    aggregate_by_page,
    build_report,
    detect,
)


def test_aggregate_by_page_sums_ratings():
    rows = [
        {"pagePath": "/a/", "customEvent:rating": "good", "eventCount": 3},
        {"pagePath": "/a/", "customEvent:rating": "bad", "eventCount": 1},
        {"pagePath": "/b/", "customEvent:rating": "bad", "eventCount": 5},
    ]
    agg = aggregate_by_page(rows, rating_available=True)
    by_path = {r["page_path"]: r for r in agg}
    assert by_path["/a/"]["total"] == 4
    assert by_path["/a/"]["good"] == 3
    assert by_path["/a/"]["bad"] == 1
    assert by_path["/a/"]["bad_ratio"] == 0.25
    assert by_path["/b/"]["bad_ratio"] == 1.0


def test_aggregate_by_page_unknown_rating_goes_to_other():
    rows = [{"pagePath": "/a/", "customEvent:rating": "weird", "eventCount": 2}]
    agg = aggregate_by_page(rows, rating_available=True)
    assert agg[0]["other"] == 2
    assert agg[0]["bad"] == 0


def test_aggregate_by_page_without_rating_dimension():
    rows = [
        {"pagePath": "/a/", "eventCount": 10},
        {"pagePath": "/b/", "eventCount": 3},
    ]
    agg = aggregate_by_page(rows, rating_available=False)
    by_path = {r["page_path"]: r for r in agg}
    assert by_path["/a/"]["total"] == 10
    assert "bad_ratio" not in by_path["/a/"]


def test_aggregate_by_page_skips_empty_path():
    rows = [{"pagePath": "", "eventCount": 5}]
    assert aggregate_by_page(rows, rating_available=False) == []


def test_detect_filters_min_total_and_sorts_by_bad_ratio():
    agg = [
        {"page_path": "/low-vol/", "total": 2, "bad_ratio": 1.0},
        {"page_path": "/worst/", "total": 10, "bad_ratio": 0.8},
        {"page_path": "/mid/", "total": 10, "bad_ratio": 0.3},
    ]
    result = detect(agg, rating_available=True, min_total=5, max_results=10)
    assert [r["page_path"] for r in result] == ["/worst/", "/mid/"]


def test_detect_caps_max_results():
    agg = [{"page_path": f"/p{i}/", "total": 10, "bad_ratio": i / 10} for i in range(20)]
    result = detect(agg, rating_available=True, min_total=1, max_results=3)
    assert len(result) == 3
    # 降順 (bad_ratio 最大が先頭)
    assert result[0]["page_path"] == "/p19/"


def test_detect_without_rating_sorts_by_total_desc():
    agg = [
        {"page_path": "/a/", "total": 5},
        {"page_path": "/b/", "total": 20},
        {"page_path": "/c/", "total": 10},
    ]
    result = detect(agg, rating_available=False, min_total=1, max_results=10)
    assert [r["page_path"] for r in result] == ["/b/", "/c/", "/a/"]


def test_build_report_end_to_end_with_rating():
    data = {
        "range": {"start": "2026-06-01", "end": "2026-06-30"},
        "rating_dimension_available": True,
        "rows": [
            {"pagePath": "/a/", "customEvent:rating": "bad", "eventCount": 8},
            {"pagePath": "/a/", "customEvent:rating": "good", "eventCount": 2},
        ],
    }
    report = build_report(data, min_total=5, max_results=5)
    assert report["rating_dimension_available"] is True
    assert report["totals"]["pages"] == 1
    assert report["totals"]["events"] == 10
    assert report["detected"][0]["page_path"] == "/a/"
    assert report["detected"][0]["bad_ratio"] == 0.8


def test_build_report_end_to_end_without_rating():
    data = {
        "range": {"start": "2026-06-01", "end": "2026-06-30"},
        "rating_dimension_available": False,
        "rows": [{"pagePath": "/a/", "eventCount": 12}],
    }
    report = build_report(data, min_total=5, max_results=5)
    assert report["rating_dimension_available"] is False
    assert report["detected"][0]["page_path"] == "/a/"
    assert "bad_ratio" not in report["detected"][0]
