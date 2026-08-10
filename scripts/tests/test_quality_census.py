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


def test_render_body_survives_missing_diff():
    import comment_quality_census as cqc
    body = cqc.render_body(_snapshot("2026-08-10", ["a"]))
    assert "初回" in body
