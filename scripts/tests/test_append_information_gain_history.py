"""scripts/append_information_gain_history.py unit tests (#4841 S3)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.append_information_gain_history import (
    build_row,
    existing_dates,
    run,
)


@pytest.fixture()
def audit_fixture():
    return {
        "generated_at": "2026-09-15T21:48:36Z",
        "model": "gemma4:26b-a4b-it-qat",
        "source_week": "2026-W38",
        "summary": {
            "target_count": 40,
            "processed_count": 38,
            "failed_count": 2,
            "failure_ratio": 0.05,
            "median_unique_and_supported_count": 6,
            "median_unique_and_supported_rate": 0.32,
            "by_experience_material": {
                "with_material": {"count": 20, "median_unique_and_supported_count": 8,
                                   "median_unique_and_supported_rate": 0.4},
                "without_material": {"count": 18, "median_unique_and_supported_count": 4,
                                      "median_unique_and_supported_rate": 0.2},
                "unknown": {"count": 0, "median_unique_and_supported_count": None,
                            "median_unique_and_supported_rate": None},
            },
            "unsupported_classification_totals": {
                "rhetorical_or_time_dependent": 30, "factual_claim": 4, "unresolved": 1,
            },
        },
    }


def test_build_row_extracts_known_fields(audit_fixture):
    row = build_row(audit_fixture)
    assert row["date"] == "2026-W38"
    assert row["target_count"] == 40
    assert row["failed_count"] == 2
    assert row["with_material"]["count"] == 20
    assert row["with_material"]["median_unique_and_supported_count"] == 8
    assert row["unsupported_classification_totals"]["rhetorical_or_time_dependent"] == 30


def test_build_row_none_when_no_source_week(audit_fixture):
    del audit_fixture["source_week"]
    assert build_row(audit_fixture) is None


def test_build_row_nulls_missing_counts_not_zero(audit_fixture, caplog):
    del audit_fixture["summary"]["target_count"]
    row = build_row(audit_fixture)
    assert row["target_count"] is None  # unknown != 0
    assert "missing or malformed" in caplog.text


def test_existing_dates_reads_jsonl(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text(json.dumps({"date": "2026-W37"}) + "\n" + json.dumps({"date": "2026-W38"}) + "\n",
                 encoding="utf-8")
    assert existing_dates(p) == {"2026-W37", "2026-W38"}


def test_existing_dates_missing_file(tmp_path):
    assert existing_dates(tmp_path / "nope.jsonl") == set()


def test_existing_dates_skips_corrupt_lines(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"date": "2026-W37"}\nnot json\n', encoding="utf-8")
    assert existing_dates(p) == {"2026-W37"}


def test_run_appends_new_week(tmp_path, audit_fixture):
    history_path = tmp_path / "information_gain_history.jsonl"
    appended, week = run(audit_fixture, history_path)
    assert appended is True
    assert week == "2026-W38"
    rows = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-W38"


def test_run_replaces_same_week_instead_of_duplicating(tmp_path, audit_fixture):
    """母艦レビュー要修正3: limit=3 のスモーク run のあとに本番 limit=40 run が来ても、
    その週の枠が最初の run に占有されず、後着の内容で置き換わること。
    """
    history_path = tmp_path / "information_gain_history.jsonl"
    smoke = dict(audit_fixture)
    smoke["limit"] = 3
    smoke["summary"] = {**audit_fixture["summary"], "target_count": 3, "processed_count": 3}
    written, week = run(smoke, history_path)
    assert written is True
    assert week == "2026-W38"

    full = dict(audit_fixture)
    full["limit"] = 40
    written, week = run(full, history_path)
    assert written is True

    rows = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1  # 重複しない
    assert rows[0]["limit"] == 40  # 最初の smoke run ではなく後着の内容が残る
    assert rows[0]["target_count"] == 40


def test_run_skips_when_run_ok_is_false(tmp_path, audit_fixture):
    """母艦レビュー要修正3: 失敗した run は history に書かない (run の赤で知らせる)。"""
    audit_fixture["run_ok"] = False
    history_path = tmp_path / "information_gain_history.jsonl"
    written, week = run(audit_fixture, history_path)
    assert written is False
    assert week == "2026-W38"
    assert not history_path.exists()


def test_run_includes_run_ok_and_limit_fields(tmp_path, audit_fixture):
    audit_fixture["run_ok"] = True
    audit_fixture["limit"] = 40
    history_path = tmp_path / "information_gain_history.jsonl"
    run(audit_fixture, history_path)
    row = json.loads(history_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["run_ok"] is True
    assert row["limit"] == 40


def test_run_returns_false_when_source_week_missing(tmp_path, audit_fixture):
    del audit_fixture["source_week"]
    written, week = run(audit_fixture, tmp_path / "h.jsonl")
    assert written is False
    assert week is None
