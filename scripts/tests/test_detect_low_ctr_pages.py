"""scripts/detect_low_ctr_pages.py unit tests (A-1, epic #1356)."""
from __future__ import annotations

import pytest

from scripts.detect_low_ctr_pages import (
    collect_top_queries_by_page,
    compute_baseline_ctr,
    detect,
)


def _fully_known_combo(by_page):
    """各ページの imp を 1 クエリで全部説明する by_combo。

    既知クエリ imp の下限 (brain#128) と無関係なテストが、その条件で落ちないようにする。
    """
    return [{"page": r["page"], "query": "q", "clicks": r.get("clicks", 0),
             "impressions": r["impressions"], "ctr": r.get("ctr", 0.0),
             "position": r.get("position", 0.0)} for r in by_page]


def _make_gsc(by_page, by_combo=None, clicks_sum=100, impressions_sum=10000,
              clicks_sitewide=None, impressions_sitewide=None):
    totals = {"clicks_sum": clicks_sum, "impressions_sum": impressions_sum}
    if impressions_sitewide is not None:
        totals["clicks_sitewide"] = clicks_sitewide
        totals["impressions_sitewide"] = impressions_sitewide
    return {
        "totals": totals,
        "by_page": by_page,
        "by_combo": _fully_known_combo(by_page) if by_combo is None else by_combo,
        "range": {"start": "2026-05-26", "end": "2026-06-01"},
    }


def test_baseline_zero_impressions_returns_zero():
    assert compute_baseline_ctr({"clicks_sum": 0, "impressions_sum": 0}) == (0.0, "fallback_topn")
    assert compute_baseline_ctr({}) == (0.0, "fallback_topn")


def test_baseline_falls_back_to_topn_sum_when_sitewide_missing():
    # amazon-navi-brain#33: *_sitewide が無い旧スナップショットのみ clicks_sum/impressions_sum を使う
    assert compute_baseline_ctr(
        {"clicks_sum": 100, "impressions_sum": 10000}
    ) == (0.01, "fallback_topn")


def test_baseline_prefers_sitewide_over_topn_sum():
    # amazon-navi-brain#33: clicks_sum/impressions_sum は top-N ページだけの合計で上振れしうるため、
    # *_sitewide (真の site 全体) がある場合はそちらを優先する
    baseline, source = compute_baseline_ctr({
        "clicks_sum": 60, "impressions_sum": 1000,  # 上振れした top-N 合計 (6.0%)
        "clicks_sitewide": 60, "impressions_sitewide": 2400,  # 真の sitewide (2.5%)
    })
    assert source == "sitewide"
    assert baseline == pytest.approx(60 / 2400)


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
    # baseline = 1000/10000 = 0.10, ratio 0.5 (明示指定) → threshold = 0.05
    # ctr=0.04 (< 0.05) は flag、ctr=0.06 (>= 0.05) は通常
    gsc = _make_gsc(by_page=[
        {"page": "/below/", "clicks": 10, "impressions": 250, "ctr": 0.04, "position": 3.0},
        {"page": "/above/", "clicks": 15, "impressions": 250, "ctr": 0.06, "position": 3.0},
    ], clicks_sum=1000, impressions_sum=10000)
    result = detect(gsc, ratio=0.5)
    assert [d["page"] for d in result["detected"]] == ["/below/"]


def test_detect_uses_sitewide_baseline_when_present():
    # amazon-navi-brain#33: clicks_sum/impressions_sum (top-N) は上振れするので、sitewide が
    # あるときはそちらを baseline に使う。ratio_to_baseline は sitewide 基準。
    gsc = _make_gsc(by_page=[
        {"page": "/p/", "clicks": 5, "impressions": 250, "ctr": 0.02, "position": 3.0},
    ], clicks_sum=60, impressions_sum=1000,
       clicks_sitewide=60, impressions_sitewide=2400)
    result = detect(gsc, ratio=0.5)
    assert result["baseline_source"] == "sitewide"
    assert result["baseline_ctr"] == pytest.approx(60 / 2400)


def test_default_ratio_is_recalibrated_above_one():
    # amazon-navi-brain#33: sitewide baseline へ切り替えた際、旧 (top-N baseline, ratio=0.5) と
    # 同等の発火頻度になるよう実データでスイープして 1.5 に再較正した。0.5 の
    # ままだと sitewide baseline は top-N baseline よりずっと低いため、発火が
    # 大きく減ってしまう (amazon-navi-brain#33 のレビューコメント参照)。
    from scripts.detect_low_ctr_pages import DEFAULT_RATIO
    assert DEFAULT_RATIO == 1.5


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


# --- 既知クエリ imp の下限 (amazon-navi-brain#128) ---------------------------

def _combo(page, impressions, query="q"):
    return {"page": page, "query": query, "clicks": 0, "impressions": impressions,
            "ctr": 0.0, "position": 5.0}


def test_known_query_gate_drops_page_with_too_few_known_impressions():
    # ページの imp は十分でも、クエリ別の行で説明できる imp が下限未満なら出さない。
    gsc = _make_gsc(
        by_page=[
            {"page": "/thin/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0},
            {"page": "/ok/", "clicks": 0, "impressions": 400, "ctr": 0.0, "position": 5.0},
        ],
        by_combo=[_combo("/thin/", 4), _combo("/ok/", 5)],
    )
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/ok/"], "下限ちょうどは残す"
    assert result["detected"][0]["known_query_impressions"] == 5
    assert result["dropped_low_known_query_imp"] == 1
    assert result["eligible"] == 2, "eligible は量のしきい値の母数のまま (落とした分も数える)"
    assert result["params"]["min_known_query_imp"] == 5


def test_known_query_gate_sums_all_combo_rows_not_only_top_queries():
    # top_queries は上位 N 件に切るが、既知 imp は全行の合計で判定する。
    gsc = _make_gsc(
        by_page=[{"page": "/p/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0}],
        by_combo=[_combo("/p/", 1, query=f"q{i}") for i in range(6)],
    )
    result = detect(gsc, top_queries_per_page=2)
    assert result["detected"][0]["known_query_impressions"] == 6
    assert len(result["detected"][0]["top_queries"]) == 2


def test_known_query_gate_applies_before_max_results_cap():
    # 落としたページの枠を、材料のあるページが使えること。
    gsc = _make_gsc(
        by_page=[
            {"page": "/big-thin/", "clicks": 0, "impressions": 900, "ctr": 0.0, "position": 5.0},
            {"page": "/small-ok/", "clicks": 0, "impressions": 100, "ctr": 0.0, "position": 5.0},
        ],
        by_combo=[_combo("/small-ok/", 30)],
    )
    result = detect(gsc, max_results=1)
    assert [d["page"] for d in result["detected"]] == ["/small-ok/"]


def test_known_query_gate_skipped_when_combo_rows_truncated():
    # rowLimit に張り付いた週は行の合計が下限値でしかないので落とさない。
    gsc = _make_gsc(
        by_page=[{"page": "/p/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0}],
        by_combo=[],
    )
    gsc["totals"]["truncated_combos"] = True
    result = detect(gsc)
    assert [d["page"] for d in result["detected"]] == ["/p/"]
    assert result["known_query_gate_applied"] is False
    assert result["dropped_low_known_query_imp"] == 0


def test_known_query_gate_disabled_with_zero():
    gsc = _make_gsc(
        by_page=[{"page": "/p/", "clicks": 0, "impressions": 500, "ctr": 0.0, "position": 5.0}],
        by_combo=[],
    )
    result = detect(gsc, min_known_query_imp=0)
    assert [d["page"] for d in result["detected"]] == ["/p/"]
