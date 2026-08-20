"""quality_census / comment_quality_census の単体テスト (#4826 項目 3)。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import quality_census as qc  # noqa: E402


def _snapshot(date: str, failing: list[str], by_check: dict | None = None) -> dict:
    return {
        "date": date,
        "articles": 100,
        "failing": len(failing),
        "failing_rate": len(failing) / 100,
        "by_check": by_check or {"how_to_choose": len(failing)},
        "by_deduction": {},
        "deduction_reasons": {},
        "score_min": 90,
        "score_median": 98,
        "score_max": 100,
        "cert_fetch": False,
        "md_evaluated": 0,
        "failing_slugs": [
            {"slug": s, "total_score": 93,
             "failed_checks": [{"name": "how_to_choose", "message": "mentions ASIN"}]}
            for s in failing
        ],
    }


# --- iter_article_paths: sidecar 除外 -------------------------------------

def test_iter_article_paths_excludes_sidecars(tmp_path: pathlib.Path):
    for name in ("2026-08-01-B0AAAAAAAA.json",
                 "2026-08-01-B0AAAAAAAA.quality.json",
                 "2026-08-01-B0AAAAAAAA.seo.json",
                 "2026-08-01-B0AAAAAAAA.enrichment.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    got = [p.name for p in qc.iter_article_paths(tmp_path)]
    assert got == ["2026-08-01-B0AAAAAAAA.json"]


# --- summarize ------------------------------------------------------------

def test_summarize_counts_failures_per_check():
    records = [
        {"slug": "a", "total_score": 98, "passed": True, "failed_checks": []},
        {"slug": "b", "total_score": 93, "passed": False,
         "failed_checks": [("how_to_choose", "x")]},
        {"slug": "c", "total_score": 90, "passed": False,
         "failed_checks": [("how_to_choose", "y"), ("tone", "z")]},
    ]
    s = qc.summarize(records, cert_fetch=False, date="2026-08-10")
    assert s["articles"] == 3
    assert s["failing"] == 2
    assert s["by_check"] == {"how_to_choose": 2, "tone": 1}
    assert s["cert_fetch"] is False
    assert [d["slug"] for d in s["failing_slugs"]] == ["b", "c"]


def test_summarize_records_cert_fetch_flag_so_ci_divergence_is_visible():
    s = qc.summarize(
        [{"slug": "a", "total_score": 98, "passed": True, "failed_checks": []}],
        cert_fetch=True, date="2026-08-10",
    )
    assert s["cert_fetch"] is True


# --- diff_against ---------------------------------------------------------

def test_diff_first_run_has_no_previous_date():
    d = qc.diff_against(None, _snapshot("2026-08-10", ["a", "b"]))
    assert d["previous_date"] is None
    assert d["new"] == ["a", "b"]
    assert d["recovered"] == [] and d["persisting"] == []


def test_diff_splits_new_recovered_persisting():
    prev = _snapshot("2026-08-03", ["a", "b"])
    cur = _snapshot("2026-08-10", ["b", "c"])
    d = qc.diff_against(prev, cur)
    assert d["previous_date"] == "2026-08-03"
    assert d["new"] == ["c"]
    assert d["recovered"] == ["a"]
    assert d["persisting"] == ["b"]


# --- history --------------------------------------------------------------

def test_append_history_is_idempotent_per_date(tmp_path: pathlib.Path):
    h = tmp_path / "quality_census.jsonl"
    row = qc.history_row(_snapshot("2026-08-10", ["a"]),
                         qc.diff_against(None, _snapshot("2026-08-10", ["a"])))
    assert qc.append_history(h, row, force=False) is True
    # 同じ date の 2 回目は skip される
    assert qc.append_history(h, row, force=False) is False
    assert len(h.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_append_history_force_replaces_same_date_row(tmp_path: pathlib.Path):
    h = tmp_path / "quality_census.jsonl"
    first = qc.history_row(_snapshot("2026-08-10", ["a"]),
                           qc.diff_against(None, _snapshot("2026-08-10", ["a"])))
    qc.append_history(h, first, force=False)
    second = qc.history_row(_snapshot("2026-08-10", ["a", "b", "c"]),
                            qc.diff_against(None, _snapshot("2026-08-10", ["a", "b", "c"])))
    assert qc.append_history(h, second, force=True) is True
    lines = [json.loads(l) for l in h.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1
    assert lines[0]["failing"] == 3


def test_append_history_keeps_other_dates_on_force(tmp_path: pathlib.Path):
    h = tmp_path / "quality_census.jsonl"
    qc.append_history(h, qc.history_row(_snapshot("2026-08-03", ["a"]),
                                        qc.diff_against(None, _snapshot("2026-08-03", ["a"]))),
                      force=False)
    qc.append_history(h, qc.history_row(_snapshot("2026-08-10", ["a"]),
                                        qc.diff_against(None, _snapshot("2026-08-10", ["a"]))),
                      force=False)
    qc.append_history(h, qc.history_row(_snapshot("2026-08-10", ["a", "b"]),
                                        qc.diff_against(None, _snapshot("2026-08-10", ["a", "b"]))),
                      force=True)
    rows = [json.loads(l) for l in h.read_text(encoding="utf-8").strip().splitlines()]
    assert sorted(r["date"] for r in rows) == ["2026-08-03", "2026-08-10"]
    assert {r["date"]: r["failing"] for r in rows}["2026-08-10"] == 2


def test_history_has_date_ignores_corrupt_lines(tmp_path: pathlib.Path):
    h = tmp_path / "quality_census.jsonl"
    h.write_text('not json\n{"date": "2026-08-10"}\n', encoding="utf-8")
    assert qc.history_has_date(h, "2026-08-10") is True
    assert qc.history_has_date(h, "2026-08-03") is False


def test_history_row_column_set_is_stable():
    """時系列として列集合を固定する (append_census_history と同方針)。"""
    a = qc.history_row(_snapshot("2026-08-03", []), qc.diff_against(None, _snapshot("2026-08-03", [])))
    b = qc.history_row(_snapshot("2026-08-10", ["x"]),
                       qc.diff_against(_snapshot("2026-08-03", []), _snapshot("2026-08-10", ["x"])))
    assert set(a) == set(b)


# --- レポート本文 ---------------------------------------------------------

def test_render_body_has_date_marker_for_dedupe():
    import comment_quality_census as cqc
    snap = _snapshot("2026-08-10", ["a"])
    snap["diff"] = qc.diff_against(None, snap)
    body = cqc.render_body(snap)
    assert "<!-- quality-census:2026-08-10 -->" in body


def test_render_body_notes_cert_fetch_divergence_when_disabled():
    import comment_quality_census as cqc
    snap = _snapshot("2026-08-10", ["a"])
    snap["diff"] = qc.diff_against(None, snap)
    assert "cert_sources_content" in cqc.render_body(snap)

    snap2 = _snapshot("2026-08-10", ["a"])
    snap2["cert_fetch"] = True
    snap2["diff"] = qc.diff_against(None, snap2)
    assert "cert_sources_content" not in cqc.render_body(snap2)


def test_render_body_reports_total_when_slug_list_truncated():
    """上限で切るときは必ず総数を残す (機能不全を不可視にしない)。"""
    import comment_quality_census as cqc
    many = [f"2026-08-10-B0{i:08d}" for i in range(cqc.SLUG_LIST_LIMIT + 5)]
    snap = _snapshot("2026-08-10", many)
    snap["diff"] = qc.diff_against(None, snap)
    body = cqc.render_body(snap)
    assert f"総数 {len(many)} 件" in body


def test_summarize_counts_deductions_separately_from_failures():
    """減点のみ (passed=True かつ score<1.0) は合否に出ないので別に数える。"""
    records = [
        {"slug": "a", "total_score": 98, "passed": True, "failed_checks": [],
         "deducted_checks": [("faq", "only 1 questions contain product name (recommend >=2)")]},
        {"slug": "b", "total_score": 97, "passed": True, "failed_checks": [],
         "deducted_checks": [("faq", "only 2 questions contain product name (recommend >=3)"),
                             ("keywords", "product name not in any keyword")]},
        {"slug": "c", "total_score": 94, "passed": False,
         "failed_checks": [("how_to_choose", "x")], "deducted_checks": []},
    ]
    s = qc.summarize(records, cert_fetch=False, date="2026-08-10")
    assert s["failing"] == 1
    assert s["by_check"] == {"how_to_choose": 1}
    assert s["by_deduction"] == {"faq": 2, "keywords": 1}


def test_deduction_reasons_collapse_embedded_numbers():
    """理由に埋まった数値で無限に分岐させない ("only 1 ..." と "only 2 ..." は同一理由)。"""
    assert (qc.normalize_reason("only 1 questions contain product name (recommend >=2)")
            == qc.normalize_reason("only 2 questions contain product name (recommend >=3)"))
    assert qc.normalize_reason("closing 92<120; daily_use 40<150") == "closing NN<NNN"


def test_summarize_keeps_deduction_reason_counts():
    records = [
        {"slug": f"a{i}", "total_score": 98, "passed": True, "failed_checks": [],
         "deducted_checks": [("keywords", "product name not in any keyword")]}
        for i in range(3)
    ] + [
        {"slug": "b", "total_score": 98, "passed": True, "failed_checks": [],
         "deducted_checks": [("keywords", "brand 'レゴ' not in any keyword")]},
    ]
    s = qc.summarize(records, cert_fetch=False, date="2026-08-10")
    assert s["by_deduction"]["keywords"] == 4
    assert s["deduction_reasons"]["keywords"]["product name not in any keyword"] == 3


def test_summarize_tolerates_records_without_deducted_checks():
    """旧スキーマのレコード (deducted_checks 無し) でも落ちない。"""
    records = [{"slug": "a", "total_score": 98, "passed": True, "failed_checks": []}]
    assert qc.summarize(records, cert_fetch=False, date="2026-08-10")["by_deduction"] == {}


def test_render_body_surfaces_deductions():
    import comment_quality_census as cqc
    snap = _snapshot("2026-08-10", ["a"])
    snap["by_deduction"] = {"faq": 595, "keywords": 512}
    snap["deduction_reasons"] = {"keywords": {"product name not in any keyword": 258}}
    snap["diff"] = qc.diff_against(None, snap)
    body = cqc.render_body(snap)
    assert "減点のみ" in body
    assert "595" in body and "512" in body
    assert "product name not in any keyword (258)" in body


def test_history_row_includes_deductions():
    snap = _snapshot("2026-08-10", ["a"])
    snap["by_deduction"] = {"faq": 595}
    row = qc.history_row(snap, qc.diff_against(None, snap))
    assert row["by_deduction"] == {"faq": 595}


def test_summarize_counts_md_evaluated_articles():
    """MD 有無は合否を変えないがスコアを動かすので、件数を残さないと環境差が無言になる。"""
    records = [
        {"slug": "a", "total_score": 98, "passed": True, "failed_checks": [], "md": True},
        {"slug": "b", "total_score": 97, "passed": True, "failed_checks": [], "md": False},
        {"slug": "c", "total_score": 96, "passed": True, "failed_checks": []},
    ]
    assert qc.summarize(records, cert_fetch=False, date="2026-08-10")["md_evaluated"] == 1


def test_render_body_flags_md_less_run_as_pass_collapsed():
    import comment_quality_census as cqc
    snap = _snapshot("2026-08-10", ["a"])
    snap["diff"] = qc.diff_against(None, snap)
    body = cqc.render_body(snap)
    assert "0 / 100 件" in body
    assert "unknown を pass に潰す" in body


def test_tracker_issue_is_pinned_not_searched():
    """番号が pin されていないと Search のインデックスラグで週次投稿が恒久 skip される。"""
    import comment_quality_census as cqc
    assert cqc.DEFAULT_TRACKER_ISSUE > 0


def test_resolve_tracker_prefers_explicit_number_over_search():
    import comment_quality_census as cqc

    def _boom(_repo):  # Search が呼ばれたら失敗させる
        raise AssertionError("Search API should not be called when a number is pinned")

    orig = cqc.find_tracker_issue_number
    cqc.find_tracker_issue_number = _boom
    try:
        assert cqc.resolve_tracker_issue_number("o/r", 4828) == 4828
    finally:
        cqc.find_tracker_issue_number = orig


def test_render_body_survives_missing_diff():
    import comment_quality_census as cqc
    body = cqc.render_body(_snapshot("2026-08-10", ["a"]))
    assert "初回" in body


# --- 直近コホート (#5083 項目3 / #4826 項目2 の昇格判定用) -----------------

def _rec(slug: str, *, passed: bool = True, deducted: list[str] | None = None) -> dict:
    return {
        "slug": slug,
        "total_score": 99 if passed else 93,
        "passed": passed,
        "md": False,
        "failed_checks": [] if passed else [("how_to_choose", "mentions ASIN")],
        "deducted_checks": [(n, "reason") for n in (deducted or [])],
    }


def test_cohort_takes_newest_n_by_slug_order():
    # slug は YYYY-MM-DD-ASIN なので辞書順 = 生成順。末尾 2 本が最新。
    records = [
        _rec("2026-08-01-B0AAAAAAAA", deducted=["keywords"]),
        _rec("2026-08-02-B0BBBBBBBB", deducted=["keywords"]),
        _rec("2026-08-03-B0CCCCCCCC"),
        _rec("2026-08-04-B0DDDDDDDD"),
    ]
    c = qc.cohort_summary(records, 2)
    assert c["from"] == "2026-08-03-B0CCCCCCCC"
    assert c["to"] == "2026-08-04-B0DDDDDDDD"
    # 古い 2 本にしか無い減点はコホートに出ない = 施行後の改善が見える
    assert c["by_deduction"] == {}


def test_cohort_is_order_independent():
    """records の並びに依存せず slug 順で切ること。"""
    records = [
        _rec("2026-08-04-B0DDDDDDDD"),
        _rec("2026-08-01-B0AAAAAAAA", deducted=["faq"]),
        _rec("2026-08-03-B0CCCCCCCC"),
        _rec("2026-08-02-B0BBBBBBBB", deducted=["faq"]),
    ]
    c = qc.cohort_summary(records, 2)
    assert (c["from"], c["to"]) == ("2026-08-03-B0CCCCCCCC", "2026-08-04-B0DDDDDDDD")
    assert c["by_deduction"] == {}


def test_cohort_none_when_corpus_smaller_than_n():
    """母集団が n 未満ならコホートは全体と同じになるので出さない。"""
    records = [_rec("2026-08-01-B0AAAAAAAA")]
    assert qc.cohort_summary(records, 100) is None


def test_cohort_rule_of_three_upper_bound():
    """発火 0 のときの 95% 上限は 3/n。#4826 項目2 の昇格目安 1.8% は n>=163。"""
    records = [_rec(f"2026-08-01-B0{i:08d}") for i in range(200)]
    c = qc.cohort_summary(records, 200)
    assert c["by_deduction"] == {}
    assert c["zero_firing_95_upper"] == pytest.approx(0.015, abs=1e-4)
    assert qc.cohort_summary(records, 100)["zero_firing_95_upper"] == pytest.approx(0.03, abs=1e-4)


def test_cohort_counts_failing_separately():
    records = [_rec(f"2026-08-01-B0{i:08d}") for i in range(9)]
    records.append(_rec("2026-08-02-B0ZZZZZZZZ", passed=False))
    c = qc.cohort_summary(records, 10)
    assert c["failing"] == 1
    assert c["failing_rate"] == pytest.approx(0.1)


def test_summarize_includes_cohorts_for_requested_sizes():
    records = [_rec(f"2026-08-01-B0{i:08d}", deducted=["keywords"] if i < 5 else None)
               for i in range(20)]
    snap = qc.summarize(records, cert_fetch=False, date="2026-08-20",
                        cohort_sizes=(5, 10, 1000))
    # 1000 はコーパス超過なので出ない
    assert set(snap["cohorts"]) == {"recent_5", "recent_10"}
    # 古い 5 本だけが減点されているので、直近コホートには出ない
    assert snap["cohorts"]["recent_10"]["by_deduction"] == {}
    assert snap["by_deduction"] == {"keywords": 5}


def test_summarize_without_cohort_sizes_emits_empty_dict():
    records = [_rec(f"2026-08-01-B0{i:08d}") for i in range(20)]
    snap = qc.summarize(records, cert_fetch=False, date="2026-08-20", cohort_sizes=())
    assert snap["cohorts"] == {}


def test_history_row_carries_cohorts():
    records = [_rec(f"2026-08-01-B0{i:08d}") for i in range(20)]
    snap = qc.summarize(records, cert_fetch=False, date="2026-08-20", cohort_sizes=(10,))
    row = qc.history_row(snap, {"previous_date": None, "new": [], "recovered": [],
                                "persisting": []})
    assert row["cohorts"]["recent_10"]["n"] == 10


def test_history_row_tolerates_snapshot_without_cohorts():
    """cohorts 導入前のスナップショットを渡しても列は落ちない。"""
    snap = _snapshot("2026-08-20", [])
    row = qc.history_row(snap, {"previous_date": None, "new": [], "recovered": [],
                                "persisting": []})
    assert row["cohorts"] == {}


def test_render_body_surfaces_cohort_table():
    from comment_quality_census import render_body
    snap = _snapshot("2026-08-20", [])
    snap["cohorts"] = {
        "recent_200": {"n": 200, "from": "2026-08-01-B0AAAAAAAA",
                       "to": "2026-08-20-B0ZZZZZZZZ", "failing": 0,
                       "failing_rate": 0.0, "by_deduction": {"keywords": 7},
                       "zero_firing_95_upper": 0.015},
    }
    body = render_body(snap)
    assert "直近コホート" in body
    assert "recent_200" in body
    assert "`keywords` 7" in body
    assert "1.50%" in body


def test_render_body_marks_cohort_with_no_deduction():
    """発火 0 は昇格の判断材料そのものなので、空欄ではなく明示すること。"""
    from comment_quality_census import render_body
    snap = _snapshot("2026-08-20", [])
    snap["cohorts"] = {
        "recent_300": {"n": 300, "from": "a", "to": "b", "failing": 0,
                       "failing_rate": 0.0, "by_deduction": {},
                       "zero_firing_95_upper": 0.01},
    }
    assert "**なし**" in render_body(snap)


def test_render_body_survives_missing_cohorts():
    """cohorts 導入前の snapshot でもレポートは壊れない。"""
    from comment_quality_census import render_body
    assert "直近コホート" not in render_body(_snapshot("2026-08-20", []))


# --- 施行日コホート (#4826 項目2 の昇格条件そのもの) -----------------------

def test_since_cohort_filters_by_slug_date():
    records = [
        _rec("2026-08-16-B0AAAAAAAA", deducted=["title_serp_fit"]),
        _rec("2026-08-17-B0BBBBBBBB", deducted=["title_serp_fit"]),
        _rec("2026-08-18-B0CCCCCCCC"),
        _rec("2026-08-19-B0DDDDDDDD"),
    ]
    c = qc.since_cohort_summary(records, "2026-08-18")
    assert c["n"] == 2
    assert c["since"] == "2026-08-18"
    assert c["by_deduction"] == {}


def test_since_cohort_includes_the_boundary_date():
    records = [_rec("2026-08-18-B0AAAAAAAA", deducted=["faq"])]
    assert qc.since_cohort_summary(records, "2026-08-18")["by_deduction"] == {"faq": 1}


def test_since_cohort_none_when_nothing_matches():
    records = [_rec("2026-08-01-B0AAAAAAAA")]
    assert qc.since_cohort_summary(records, "2026-09-01") is None


def test_since_cohort_upper_bound_tracks_actual_n():
    """直近 N 本と違い n が可変なので、上限は実際の件数から出すこと。"""
    records = [_rec(f"2026-08-18-B0{i:08d}") for i in range(50)]
    c = qc.since_cohort_summary(records, "2026-08-18")
    assert c["n"] == 50
    assert c["zero_firing_95_upper"] == pytest.approx(0.06, abs=1e-4)


def test_summarize_adds_since_cohort_alongside_recent():
    records = [_rec(f"2026-08-01-B0{i:08d}", deducted=["title_serp_fit"])
               for i in range(20)]
    records += [_rec(f"2026-08-18-B0{i:08d}") for i in range(10)]
    snap = qc.summarize(records, cert_fetch=False, date="2026-08-20",
                        cohort_sizes=(10,), since="2026-08-18")
    assert set(snap["cohorts"]) == {"recent_10", "since_2026-08-18"}
    assert snap["cohorts"]["since_2026-08-18"]["n"] == 10
    assert snap["cohorts"]["since_2026-08-18"]["by_deduction"] == {}


def test_summarize_without_since_has_no_since_cohort():
    records = [_rec(f"2026-08-01-B0{i:08d}") for i in range(20)]
    snap = qc.summarize(records, cert_fetch=False, date="2026-08-20",
                        cohort_sizes=(10,))
    assert set(snap["cohorts"]) == {"recent_10"}


def test_render_body_shows_cohort_n():
    """n を出さないと 95% 上限の根拠が読めない。"""
    from comment_quality_census import render_body
    snap = _snapshot("2026-08-20", [])
    snap["cohorts"] = {
        "since_2026-08-18": {"n": 50, "since": "2026-08-18", "from": "a", "to": "b",
                             "failing": 0, "failing_rate": 0.0, "by_deduction": {},
                             "zero_firing_95_upper": 0.06},
    }
    body = render_body(snap)
    assert "| 50 |" in body
    assert "6.00%" in body
