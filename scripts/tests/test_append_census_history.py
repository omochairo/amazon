"""scripts/append_census_history.py unit tests (B-3, #3988 / #3331 / #3333 / #2701)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.append_census_history import (
    CENSUS_HISTORY_FILE,
    KNOWN_SLUGS,
    build_row,
    existing_dates,
    map_coverage_states,
    run,
)


@pytest.fixture()
def census_fixture():
    return {
        "fetched_at": "2026-07-19T22:57:54.878408+00:00",
        "site_url": "sc-domain:omcha.jp",
        "sitemap": "https://navi.omcha.jp/sitemap.xml",
        "prefix": "/products/",
        "totals": {
            "sitemap_urls": 1592,
            "inspected": 1592,
            "errors": 2,
            "indexed": 1137,
            "not_indexed": 453,
        },
        "by_coverage_state": {
            "送信して登録されました": 1137,
            "検出 - インデックス未登録": 185,
            "URL が Google に認識されていません": 103,
            "クロール済み - インデックス未登録": 90,
            "見つかりませんでした（404）": 74,
            "noindex タグによって除外されました": 1,
        },
        "by_verdict": {},
        "by_robots_txt_state": {},
        "by_indexing_state": {},
        "not_indexed_urls": [],
        "errors": [],
        "circuit_breaker": {"tripped": False, "threshold": 8},
    }


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# coverage state slug mapping (D1)
# ---------------------------------------------------------------------------

def test_all_six_known_states_map_to_slugs(census_fixture):
    slug_counts, unmapped = map_coverage_states(census_fixture["by_coverage_state"])
    assert slug_counts["submitted_and_indexed"] == 1137
    assert slug_counts["discovered_not_indexed"] == 185
    assert slug_counts["unknown_to_google"] == 103
    assert slug_counts["crawled_not_indexed"] == 90
    assert slug_counts["not_found_404"] == 74
    assert slug_counts["excluded_by_noindex"] == 1
    assert slug_counts["other"] == 0
    assert unmapped == {}


def test_every_known_slug_present_in_row_even_when_absent():
    slug_counts, _ = map_coverage_states({"送信して登録されました": 10})
    for slug in KNOWN_SLUGS:
        assert slug in slug_counts
    assert slug_counts["not_found_404"] == 0


def test_unknown_state_lands_in_other_and_unmapped():
    by_state = {
        "送信して登録されました": 10,
        "謎の新しい状態": 3,
    }
    slug_counts, unmapped = map_coverage_states(by_state)
    assert slug_counts["other"] == 3
    assert unmapped == {"謎の新しい状態": 3}
    assert slug_counts["submitted_and_indexed"] == 10


def test_build_row_includes_all_known_slugs_as_columns(census_fixture):
    row = build_row(census_fixture)
    for slug in KNOWN_SLUGS:
        assert slug in row
    assert "other" in row
    assert row["unmapped"] == {}


# ---------------------------------------------------------------------------
# row shape / totals (D2)
# ---------------------------------------------------------------------------

def test_build_row_basic_fields(census_fixture):
    row = build_row(census_fixture)
    assert row["date"] == "2026-07-19"
    assert row["sitemap_urls"] == 1592
    assert row["inspected"] == 1592
    assert row["indexed"] == 1137
    assert row["not_indexed"] == 453
    assert row["errors"] == 2
    assert row["indexed_rate"] == 0.7142
    assert row["circuit_breaker_tripped"] is False


def test_indexed_rate_zero_when_inspected_zero(census_fixture):
    census_fixture["totals"]["inspected"] = 0
    census_fixture["totals"]["indexed"] = 0
    row = build_row(census_fixture)
    assert row["indexed_rate"] == 0.0


def test_missing_totals_fields_default_to_zero(census_fixture):
    census_fixture["totals"] = {}
    row = build_row(census_fixture)
    assert row["sitemap_urls"] == 0
    assert row["inspected"] == 0
    assert row["indexed"] == 0
    assert row["not_indexed"] == 0
    assert row["errors"] == 0
    assert row["indexed_rate"] == 0.0


def test_missing_by_coverage_state_defaults_to_zeros(census_fixture):
    del census_fixture["by_coverage_state"]
    row = build_row(census_fixture)
    for slug in KNOWN_SLUGS:
        assert row[slug] == 0
    assert row["other"] == 0
    assert row["unmapped"] == {}


def test_build_row_no_fetched_at_returns_none(census_fixture):
    del census_fixture["fetched_at"]
    assert build_row(census_fixture) is None


# ---------------------------------------------------------------------------
# circuit breaker flag (D3)
# ---------------------------------------------------------------------------

def test_circuit_breaker_tripped_true(census_fixture):
    census_fixture["circuit_breaker"] = {"tripped": True, "threshold": 8}
    row = build_row(census_fixture)
    assert row["circuit_breaker_tripped"] is True


def test_circuit_breaker_tripped_false_when_missing(census_fixture):
    del census_fixture["circuit_breaker"]
    row = build_row(census_fixture)
    assert row["circuit_breaker_tripped"] is False


# ---------------------------------------------------------------------------
# idempotency without shared sidecar (D4)
# ---------------------------------------------------------------------------

def test_existing_dates_missing_file_returns_empty(tmp_path):
    assert existing_dates(tmp_path / "nope.jsonl") == set()


def test_existing_dates_tolerates_corrupt_line(tmp_path):
    path = tmp_path / CENSUS_HISTORY_FILE
    path.write_text(
        '{"date": "2026-07-19", "x": 1}\nnot json\n{"date": "2026-07-26", "x": 2}\n',
        encoding="utf-8",
    )
    assert existing_dates(path) == {"2026-07-19", "2026-07-26"}


def test_existing_dates_tolerates_empty_file(tmp_path):
    path = tmp_path / CENSUS_HISTORY_FILE
    path.write_text("", encoding="utf-8")
    assert existing_dates(path) == set()


def test_run_appends_one_line(tmp_path, census_fixture):
    appended, target_date = run(census_fixture, tmp_path)
    assert appended is True
    assert target_date == "2026-07-19"
    rows = _read_jsonl(tmp_path / CENSUS_HISTORY_FILE)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-19"


def test_run_same_date_twice_appends_once(tmp_path, census_fixture):
    run(census_fixture, tmp_path)
    appended, target_date = run(census_fixture, tmp_path)
    assert appended is False
    assert target_date == "2026-07-19"
    rows = _read_jsonl(tmp_path / CENSUS_HISTORY_FILE)
    assert len(rows) == 1


def test_run_two_different_dates_accumulate(tmp_path, census_fixture):
    run(census_fixture, tmp_path)
    census2 = json.loads(json.dumps(census_fixture))
    census2["fetched_at"] = "2026-07-26T22:57:54.878408+00:00"
    run(census2, tmp_path)
    rows = _read_jsonl(tmp_path / CENSUS_HISTORY_FILE)
    dates = {row["date"] for row in rows}
    assert dates == {"2026-07-19", "2026-07-26"}
    assert len(rows) == 2


def test_run_no_fetched_at_skips(tmp_path, census_fixture):
    del census_fixture["fetched_at"]
    appended, target_date = run(census_fixture, tmp_path)
    assert appended is False
    assert target_date is None
    assert not (tmp_path / CENSUS_HISTORY_FILE).exists()
