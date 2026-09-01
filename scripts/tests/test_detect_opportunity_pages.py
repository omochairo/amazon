"""scripts/detect_opportunity_pages.py unit tests (A-2, epic #1356)."""
from __future__ import annotations

from scripts.detect_opportunity_pages import detect


def _make_gsc(by_page, by_combo=None):
    return {
        "by_page": by_page,
        "by_combo": by_combo or [],
        "range": {"start": "2026-05-26", "end": "2026-06-01"},
    }


def test_detect_picks_page_2_high_imp():
    gsc = _make_gsc(by_page=[
        {"page": "/p1/", "clicks": 5, "impressions": 200, "ctr": 0.025, "position": 13.5},
    ])
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/p1/"]


def test_detect_excludes_page_1():
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 5, "impressions": 200, "ctr": 0.025, "position": 8.0},
    ])
    result = detect(gsc)
    assert result["detected"] == []


def test_detect_excludes_page_3_and_beyond():
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 21.0},
        {"page": "/b/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 30.0},
    ])
    result = detect(gsc)
    assert result["detected"] == []


def test_detect_excludes_low_impressions():
    # 既定は 2026-09-01 の較正で 100 -> 20 (#5941 / amazon-navi-brain#18)。
    # 「閾値未満は落ちる」という性質を見るテストなので、既定より下の値で確かめる。
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 19, "ctr": 0.0, "position": 15.0},
    ])
    result = detect(gsc)
    assert result["detected"] == []
    assert result["eligible"] == 0


def test_detect_includes_at_calibrated_default():
    # 較正前の既定 (100) では落ちていた帯が拾えること。ここが戻ると較正が消える。
    gsc = _make_gsc(by_page=[
        {"page": "/a/", "clicks": 0, "impressions": 20, "ctr": 0.0, "position": 15.0},
    ])
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/a/"]


def test_eligible_counts_volume_gate_not_selection():
    # eligible は「量のしきい値を通った母数」で、位置窓の選別より前。
    # eligible > 0 かつ detected == 0 は「母数はあるが選別で落ちた」を意味する。
    gsc = _make_gsc(by_page=[
        {"page": "/in/", "clicks": 0, "impressions": 30, "ctr": 0.0, "position": 15.0},
        {"page": "/out/", "clicks": 0, "impressions": 30, "ctr": 0.0, "position": 3.0},
        {"page": "/thin/", "clicks": 0, "impressions": 5, "ctr": 0.0, "position": 15.0},
    ])
    result = detect(gsc)
    assert result["eligible"] == 2, "位置窓で落ちたページも母数には数える"
    assert [d["page"] for d in result["detected"]] == ["/in/"]


def test_detect_sorts_by_impressions_desc_and_caps():
    gsc = _make_gsc(by_page=[
        {"page": f"/p{i}/", "clicks": 0, "impressions": 100 + i*10, "ctr": 0.0, "position": 15.0}
        for i in range(10)
    ])
    result = detect(gsc, max_results=3)
    assert [d["page"] for d in result["detected"]] == ["/p9/", "/p8/", "/p7/"]


def test_detect_attaches_top_queries():
    gsc = _make_gsc(
        by_page=[
            {"page": "/p/", "clicks": 1, "impressions": 200, "ctr": 0.005, "position": 14.0},
        ],
        by_combo=[
            {"page": "/p/", "query": "q1", "clicks": 0, "impressions": 80, "ctr": 0.0, "position": 14.0},
            {"page": "/p/", "query": "q2", "clicks": 0, "impressions": 120, "ctr": 0.0, "position": 14.0},
        ],
    )
    result = detect(gsc, top_queries_per_page=5)
    queries = result["detected"][0]["top_queries"]
    assert [q["query"] for q in queries] == ["q2", "q1"]


def test_detect_boundary_position_11_and_20_inclusive():
    gsc = _make_gsc(by_page=[
        {"page": "/lo/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 11.0},
        {"page": "/hi/", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 20.0},
    ])
    result = detect(gsc)
    pages = sorted(d["page"] for d in result["detected"])
    assert pages == ["/hi/", "/lo/"]
