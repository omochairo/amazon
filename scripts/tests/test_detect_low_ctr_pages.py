"""scripts/detect_low_ctr_pages.py unit tests (A-1, epic #1356)."""
from __future__ import annotations

import pytest

import json

from scripts.detect_low_ctr_pages import (
    MIN_BAND_IMPRESSIONS,
    collect_top_queries_by_page,
    compute_baseline_ctr,
    detect,
    load_band_ctr,
    poisson_lower_tail,
    position_band,
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


# --- 順位帯の期待 CTR + ポアソン検定 (brain#128 の続き) ---

BANDS = {
    "1-3": {"clicks": 120, "impressions": 1000, "ctr": 0.12},
    "3-5": {"clicks": 110, "impressions": 1000, "ctr": 0.11},
    "5-7": {"clicks": 80, "impressions": 1000, "ctr": 0.08},
    "7-10": {"clicks": 45, "impressions": 1000, "ctr": 0.045},
}


def test_position_band_edges():
    assert position_band(1.0) == "1-3"
    assert position_band(3.0) == "1-3"
    assert position_band(3.01) == "3-5"
    assert position_band(10.0) == "7-10"
    assert position_band(10.5) is None
    assert position_band(0.0) is None


def test_poisson_lower_tail_matches_closed_form():
    import math
    assert poisson_lower_tail(0, 2.0) == pytest.approx(math.exp(-2.0))
    assert poisson_lower_tail(1, 2.0) == pytest.approx(3 * math.exp(-2.0))
    assert poisson_lower_tail(3, 0.0) == 1.0


def test_band_rule_does_not_flag_zero_clicks_explained_by_chance():
    # 9 位・imp 50・0 クリック: 期待 2.25 クリックで 0 は 1 割ほど起きる。旧判定なら鳴る
    page = {"page": "/p/a", "impressions": 50, "clicks": 0, "ctr": 0.0, "position": 9.0}
    gsc = _make_gsc([page], clicks_sitewide=300, impressions_sitewide=10000)
    assert len(detect(gsc)["detected"]) == 1
    assert detect(gsc, band_ctr=BANDS)["detected"] == []


def test_band_rule_flags_top_position_page_the_old_line_missed():
    # 2 位・CTR 4% は site 全体 3% × 1.5 = 4.5% の線なら鳴るが、上位で 12% 期待なら強く低い
    page = {"page": "/p/b", "impressions": 200, "clicks": 9, "ctr": 0.045, "position": 2.0}
    gsc = _make_gsc([page], clicks_sitewide=300, impressions_sitewide=10000)
    assert detect(gsc)["detected"] == []  # 4.5% は線ちょうどで鳴らない
    got = detect(gsc, band_ctr=BANDS)["detected"]
    assert [r["page"] for r in got] == ["/p/b"]
    assert got[0]["rule"] == "position_poisson"
    assert got[0]["position_band"] == "1-3"
    assert got[0]["expected_ctr"] == 0.12
    assert got[0]["p_value"] < 0.05


def test_thin_band_falls_back_to_sitewide_ratio():
    thin = {**BANDS, "7-10": {"clicks": 1, "impressions": MIN_BAND_IMPRESSIONS - 1, "ctr": 0.01}}
    page = {"page": "/p/a", "impressions": 50, "clicks": 0, "ctr": 0.0, "position": 9.0}
    gsc = _make_gsc([page], clicks_sitewide=300, impressions_sitewide=10000)
    got = detect(gsc, band_ctr=thin)
    assert [r["rule"] for r in got["detected"]] == ["sitewide_ratio"]
    assert "7-10" not in got["band_ctr"]


def test_tested_pages_sort_by_p_value_first():
    pages = [
        {"page": "/p/weak", "impressions": 400, "clicks": 10, "ctr": 0.025, "position": 8.0},
        {"page": "/p/strong", "impressions": 100, "clicks": 0, "ctr": 0.0, "position": 2.0},
    ]
    gsc = _make_gsc(pages, clicks_sitewide=300, impressions_sitewide=10000)
    got = detect(gsc, band_ctr=BANDS)["detected"]
    assert [r["page"] for r in got] == ["/p/strong", "/p/weak"]


def test_load_band_ctr_excludes_the_judged_week(tmp_path):
    def snap(name, start, end, clicks):
        (tmp_path / name).write_text(json.dumps({
            "range": {"start": start, "end": end},
            "by_page": [{"page": "/p", "clicks": clicks, "impressions": 100, "position": 2.0},
                        {"page": "/q", "clicks": 1, "impressions": 100, "position": 15.0}],
        }), encoding="utf-8")
    snap("2026-W40.json", "2026-09-24", "2026-10-01", 10)
    snap("2026-W41.json", "2026-10-01", "2026-10-08", 30)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    got = load_band_ctr(tmp_path, exclude_range={"start": "2026-10-01", "end": "2026-10-08"})
    assert got == {"1-3": {"clicks": 10, "impressions": 100, "ctr": 0.1}}  # 15 位は帯の外
    assert load_band_ctr(tmp_path)["1-3"]["clicks"] == 40
