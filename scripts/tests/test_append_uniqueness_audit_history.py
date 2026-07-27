"""scripts/append_uniqueness_audit_history.py unit tests (#4098 / #3203 Phase 3)."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

from scripts.append_uniqueness_audit_history import (
    KNOWN_COHORTS,
    PERCENTILE_FIELDS,
    UNIQUENESS_HISTORY_FILE,
    build_cohort_stats,
    build_row,
    existing_dates,
    run,
)


@pytest.fixture()
def audit_fixture():
    return {
        "generated_at": "2026-07-26T21:48:36Z",
        "model": "cl-nagoya/ruri-v3-310m",
        "source_week": "2026-W30",
        "corpus_size": 1705,
        "thresholds": {
            "mode": "percentile",
            "max_sim": 0.9762,
            "centroid_sim": 0.9422,
            "absolute_reference": {
                "max_sim": 0.95,
                "centroid_sim": 0.9,
                "max_sim_exceeded": 631,
                "centroid_sim_exceeded": 1509,
            },
            "max_sim_percentile": 95.0,
            "centroid_sim_percentile": 95.0,
        },
        "flagged_total": 160,
        "flagged_truncated": True,
        "cohort_stats": {
            "pre_v7": {
                "count": 1499,
                "max_sim_p25": 0.9247,
                "centroid_sim_p25": 0.9138,
                "max_sim_p50": 0.9418,
                "centroid_sim_p50": 0.9242,
                "max_sim_p75": 0.9579,
                "centroid_sim_p75": 0.9326,
                "max_sim_p90": 0.9696,
                "centroid_sim_p90": 0.9388,
            },
            "post_v7": {
                "count": 206,
                "max_sim_p25": 0.9265,
                "centroid_sim_p25": 0.8962,
                "max_sim_p50": 0.942,
                "centroid_sim_p50": 0.9152,
                "max_sim_p75": 0.959,
                "centroid_sim_p75": 0.9274,
                "max_sim_p90": 0.9673,
                "centroid_sim_p90": 0.9368,
            },
            "all": {
                "count": 1705,
                "max_sim_p25": 0.9249,
                "centroid_sim_p25": 0.9126,
                "max_sim_p50": 0.9418,
                "centroid_sim_p50": 0.9231,
                "max_sim_p75": 0.9581,
                "centroid_sim_p75": 0.9322,
                "max_sim_p90": 0.9693,
                "centroid_sim_p90": 0.9384,
            },
        },
        "flagged": [],
    }


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# row shape / basic fields
# ---------------------------------------------------------------------------

def test_build_row_basic_fields(audit_fixture):
    row = build_row(audit_fixture)
    assert row["date"] == "2026-W30"
    assert row["generated_at"] == "2026-07-26T21:48:36Z"
    assert row["model"] == "cl-nagoya/ruri-v3-310m"
    assert row["corpus_size"] == 1705
    assert row["flagged_total"] == 160


def test_build_row_primary_metrics_from_absolute_reference(audit_fixture):
    row = build_row(audit_fixture)
    assert row["max_sim_exceeded"] == 631
    assert row["centroid_sim_exceeded"] == 1509


def test_build_row_effective_thresholds(audit_fixture):
    row = build_row(audit_fixture)
    assert row["threshold_mode"] == "percentile"
    assert row["threshold_max_sim"] == 0.9762
    assert row["threshold_centroid_sim"] == 0.9422


def test_build_row_no_source_week_returns_none(audit_fixture):
    del audit_fixture["source_week"]
    assert build_row(audit_fixture) is None


def test_build_row_empty_source_week_returns_none(audit_fixture):
    audit_fixture["source_week"] = ""
    assert build_row(audit_fixture) is None


# ---------------------------------------------------------------------------
# unknown != pass (missing primary metrics must NOT default to 0)
# ---------------------------------------------------------------------------

def test_missing_absolute_reference_yields_null_not_zero(audit_fixture):
    del audit_fixture["thresholds"]["absolute_reference"]
    row = build_row(audit_fixture)
    assert row["max_sim_exceeded"] is None
    assert row["centroid_sim_exceeded"] is None


def test_missing_thresholds_block_yields_null_not_zero(audit_fixture):
    del audit_fixture["thresholds"]
    row = build_row(audit_fixture)
    assert row["max_sim_exceeded"] is None
    assert row["centroid_sim_exceeded"] is None
    assert row["threshold_mode"] is None
    assert row["threshold_max_sim"] is None
    assert row["threshold_centroid_sim"] is None


def test_missing_corpus_size_and_flagged_total_yield_null_not_zero(audit_fixture):
    del audit_fixture["corpus_size"]
    del audit_fixture["flagged_total"]
    row = build_row(audit_fixture)
    assert row["corpus_size"] is None
    assert row["flagged_total"] is None


def test_malformed_exceeded_type_yields_null(audit_fixture):
    audit_fixture["thresholds"]["absolute_reference"]["max_sim_exceeded"] = "not-a-number"
    row = build_row(audit_fixture)
    assert row["max_sim_exceeded"] is None


# ---------------------------------------------------------------------------
# cohort_stats column stability
# ---------------------------------------------------------------------------

def test_build_cohort_stats_all_known_cohorts_present(audit_fixture):
    stats = build_cohort_stats(audit_fixture["cohort_stats"])
    for cohort in KNOWN_COHORTS:
        assert cohort in stats
        for field in PERCENTILE_FIELDS:
            assert field in stats[cohort]
        assert "count" in stats[cohort]


def test_build_cohort_stats_values_roundtrip(audit_fixture):
    stats = build_cohort_stats(audit_fixture["cohort_stats"])
    assert stats["pre_v7"]["count"] == 1499
    assert stats["pre_v7"]["max_sim_p50"] == 0.9418
    assert stats["all"]["centroid_sim_p90"] == 0.9384


def test_build_cohort_stats_missing_cohort_defaults_count_zero_percentiles_null():
    stats = build_cohort_stats({"pre_v7": {"count": 5, "max_sim_p50": 0.9}})
    # post_v7 / all entirely absent from input
    for cohort in ("post_v7", "all"):
        assert stats[cohort]["count"] == 0
        for field in PERCENTILE_FIELDS:
            assert stats[cohort][field] is None


def test_build_cohort_stats_none_input_still_produces_all_known_cohorts():
    stats = build_cohort_stats(None)
    assert set(stats.keys()) == set(KNOWN_COHORTS)
    for cohort in KNOWN_COHORTS:
        assert stats[cohort]["count"] == 0
        for field in PERCENTILE_FIELDS:
            assert stats[cohort][field] is None


def test_build_cohort_stats_malformed_cohort_shape_treated_as_missing():
    stats = build_cohort_stats({"pre_v7": "not-a-dict"})
    assert stats["pre_v7"]["count"] == 0
    assert stats["pre_v7"]["max_sim_p50"] is None


def test_build_row_missing_cohort_stats_key_still_has_all_known_cohorts(audit_fixture):
    del audit_fixture["cohort_stats"]
    row = build_row(audit_fixture)
    assert set(row["cohort_stats"].keys()) == set(KNOWN_COHORTS)


# ---------------------------------------------------------------------------
# idempotency without shared sidecar (D4)
# ---------------------------------------------------------------------------

def test_existing_dates_missing_file_returns_empty(tmp_path):
    assert existing_dates(tmp_path / "nope.jsonl") == set()


def test_existing_dates_tolerates_corrupt_line(tmp_path):
    path = tmp_path / UNIQUENESS_HISTORY_FILE
    path.write_text(
        '{"date": "2026-W29", "x": 1}\nnot json\n{"date": "2026-W30", "x": 2}\n',
        encoding="utf-8",
    )
    assert existing_dates(path) == {"2026-W29", "2026-W30"}


def test_existing_dates_tolerates_empty_file(tmp_path):
    path = tmp_path / UNIQUENESS_HISTORY_FILE
    path.write_text("", encoding="utf-8")
    assert existing_dates(path) == set()


def test_run_appends_one_line(tmp_path, audit_fixture):
    appended, target_week = run(audit_fixture, tmp_path)
    assert appended is True
    assert target_week == "2026-W30"
    rows = _read_jsonl(tmp_path / UNIQUENESS_HISTORY_FILE)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-W30"


def test_run_same_source_week_twice_appends_once(tmp_path, audit_fixture):
    run(audit_fixture, tmp_path)
    appended, target_week = run(audit_fixture, tmp_path)
    assert appended is False
    assert target_week == "2026-W30"
    rows = _read_jsonl(tmp_path / UNIQUENESS_HISTORY_FILE)
    assert len(rows) == 1


def test_run_two_different_weeks_accumulate(tmp_path, audit_fixture):
    run(audit_fixture, tmp_path)
    audit2 = json.loads(json.dumps(audit_fixture))
    audit2["source_week"] = "2026-W31"
    run(audit2, tmp_path)
    rows = _read_jsonl(tmp_path / UNIQUENESS_HISTORY_FILE)
    weeks = {row["date"] for row in rows}
    assert weeks == {"2026-W30", "2026-W31"}
    assert len(rows) == 2


def test_run_no_source_week_skips(tmp_path, audit_fixture):
    del audit_fixture["source_week"]
    appended, target_week = run(audit_fixture, tmp_path)
    assert appended is False
    assert target_week is None
    assert not (tmp_path / UNIQUENESS_HISTORY_FILE).exists()


def test_run_idempotent_rerun_does_not_touch_jsonl(tmp_path, audit_fixture):
    run(audit_fixture, tmp_path)
    jsonl_before = (tmp_path / UNIQUENESS_HISTORY_FILE).read_text(encoding="utf-8")

    # Same source_week but different (bogus) content — if idempotency were broken
    # this would silently duplicate/alter the row for that week.
    audit_dup = json.loads(json.dumps(audit_fixture))
    audit_dup["flagged_total"] = 99999
    appended, target_week = run(audit_dup, tmp_path)

    assert appended is False
    assert target_week == "2026-W30"
    assert (tmp_path / UNIQUENESS_HISTORY_FILE).read_text(encoding="utf-8") == jsonl_before


# ---------------------------------------------------------------------------
# CLI-level graceful handling of missing/corrupt input (module-level, via main())
# ---------------------------------------------------------------------------

def test_main_missing_input_file_returns_zero_and_writes_nothing(tmp_path, monkeypatch):
    import scripts.append_uniqueness_audit_history as mod

    missing_path = tmp_path / "does_not_exist.json"
    history_dir = tmp_path / "history"
    monkeypatch.setattr(
        sys, "argv",
        ["append_uniqueness_audit_history.py",
         "--uniqueness-audit", str(missing_path),
         "--history-dir", str(history_dir)],
    )
    rc = mod.main()
    assert rc == 0
    assert not (history_dir / UNIQUENESS_HISTORY_FILE).exists()


def test_main_corrupt_json_input_returns_zero_and_writes_nothing(tmp_path, monkeypatch):
    import scripts.append_uniqueness_audit_history as mod

    bad_path = tmp_path / "uniqueness_audit.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    history_dir = tmp_path / "history"
    monkeypatch.setattr(
        sys, "argv",
        ["append_uniqueness_audit_history.py",
         "--uniqueness-audit", str(bad_path),
         "--history-dir", str(history_dir)],
    )
    rc = mod.main()
    assert rc == 0
    assert not (history_dir / UNIQUENESS_HISTORY_FILE).exists()
