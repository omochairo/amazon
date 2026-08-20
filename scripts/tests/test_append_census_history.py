"""scripts/append_census_history.py unit tests (B-3, #3988 / #3331 / #3333 / #2701)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.append_census_history import (
    CENSUS_HISTORY_FILE,
    CENSUS_URL_STATES_FILE,
    KNOWN_SLUGS,
    build_current_url_states,
    build_row,
    compute_cohort,
    discover_existing_asins,
    existing_dates,
    load_url_states_snapshot,
    map_coverage_states,
    run,
    save_url_states_snapshot,
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


# ---------------------------------------------------------------------------
# cohort transitions (#2687)
# ---------------------------------------------------------------------------

def test_build_current_url_states_maps_raw_to_slug():
    states = build_current_url_states([
        {"url": "https://navi.omcha.jp/products/b0000000a1/", "coverage_state": "URL が Google に認識されていません"},
        {"url": "https://navi.omcha.jp/products/b0000000a2/", "coverage_state": "検出 - インデックス未登録"},
    ])
    assert states == {
        "https://navi.omcha.jp/products/b0000000a1/": "unknown_to_google",
        "https://navi.omcha.jp/products/b0000000a2/": "discovered_not_indexed",
    }


def test_build_current_url_states_unknown_raw_becomes_other():
    states = build_current_url_states([
        {"url": "https://navi.omcha.jp/products/b0000000a1/", "coverage_state": "謎の新しい状態"},
    ])
    assert states["https://navi.omcha.jp/products/b0000000a1/"] == "other"


def test_build_current_url_states_skips_items_without_url():
    states = build_current_url_states([{"coverage_state": "検出 - インデックス未登録"}])
    assert states == {}


def test_no_previous_snapshot_returns_only_previous_date_none():
    cohort = compute_cohort(current_states={}, previous_snapshot=None, existing_asins=set())
    assert cohort == {"previous_date": None}


def test_compute_cohort_transition_matrix_and_sort_order():
    previous_states = {
        "a1": "unknown_to_google",
        "a2": "unknown_to_google",
        "a3": "unknown_to_google",
        "b1": "discovered_not_indexed",
        "b2": "discovered_not_indexed",
    }
    current_states = {
        "a1": "discovered_not_indexed",
        "a2": "discovered_not_indexed",
        "a3": "crawled_not_indexed",
        "b1": "discovered_not_indexed",
        # b2 exits (not present)
        "c1": "unknown_to_google",  # newly entered
    }
    previous_snapshot = {"date": "2026-07-19", "states": previous_states}
    cohort = compute_cohort(current_states, previous_snapshot, existing_asins=set())

    assert cohort["previous_date"] == "2026-07-19"
    assert cohort["previous_not_indexed"] == 5
    assert cohort["exited_not_indexed"] == 1
    assert cohort["entered_not_indexed"] == 1
    # count desc, then key asc for ties
    assert list(cohort["transitions"].items()) == [
        ("unknown_to_google->discovered_not_indexed", 2),
        ("discovered_not_indexed->discovered_not_indexed", 1),
        ("unknown_to_google->crawled_not_indexed", 1),
    ]


def test_compute_cohort_staying_in_same_slug_is_recorded():
    previous_snapshot = {"date": "2026-07-19", "states": {"a1": "discovered_not_indexed"}}
    cohort = compute_cohort(
        current_states={"a1": "discovered_not_indexed"},
        previous_snapshot=previous_snapshot,
        existing_asins=set(),
    )
    assert cohort["transitions"] == {"discovered_not_indexed->discovered_not_indexed": 1}
    assert cohort["exited_not_indexed"] == 0
    assert cohort["entered_not_indexed"] == 0


def test_compute_cohort_exit_classified_indexed_when_asin_exists():
    previous_snapshot = {
        "date": "2026-07-19",
        "states": {
            "https://navi.omcha.jp/products/b0000000a1/": "unknown_to_google",
            "https://navi.omcha.jp/products/b0000000a2/": "unknown_to_google",
        },
    }
    cohort = compute_cohort(
        current_states={},
        previous_snapshot=previous_snapshot,
        existing_asins={"B0000000A1"},
    )
    assert cohort["exited_not_indexed"] == 2
    assert cohort["exited_indexed"] == 1
    assert cohort["exited_dropped"] == 1


def test_compute_cohort_articles_dir_unavailable_omits_exit_classification_keys():
    previous_snapshot = {
        "date": "2026-07-19",
        "states": {"https://navi.omcha.jp/products/b0000000a1/": "unknown_to_google"},
    }
    cohort = compute_cohort(current_states={}, previous_snapshot=previous_snapshot, existing_asins=None)
    assert cohort["exited_not_indexed"] == 1
    assert "exited_indexed" not in cohort
    assert "exited_dropped" not in cohort


# ---------------------------------------------------------------------------
# article existence / sidecar exclusion (#2687)
# ---------------------------------------------------------------------------

def test_discover_existing_asins_finds_article_body(tmp_path):
    (tmp_path / "2026-05-13-B0FHVH6JML.json").write_text("{}", encoding="utf-8")
    asins = discover_existing_asins(tmp_path)
    assert asins == {"B0FHVH6JML"}


def test_discover_existing_asins_excludes_sidecar_suffixes(tmp_path):
    (tmp_path / "2026-05-13-B0FHVH6JML.quality.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-05-13-B0FHVH6JML.seo.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-05-13-B0FHVH6JML.enrichment.json").write_text("{}", encoding="utf-8")
    asins = discover_existing_asins(tmp_path)
    assert asins == set()


def test_discover_existing_asins_missing_dir_returns_none(tmp_path):
    assert discover_existing_asins(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# snapshot load/save round trip (#2687)
# ---------------------------------------------------------------------------

def test_snapshot_round_trip(tmp_path):
    path = tmp_path / CENSUS_URL_STATES_FILE
    assert load_url_states_snapshot(path) is None
    save_url_states_snapshot(path, "2026-07-19", {"u1": "unknown_to_google"})
    loaded = load_url_states_snapshot(path)
    assert loaded == {"date": "2026-07-19", "states": {"u1": "unknown_to_google"}}


def test_snapshot_corrupt_file_treated_as_no_previous(tmp_path):
    path = tmp_path / CENSUS_URL_STATES_FILE
    path.write_text("not json", encoding="utf-8")
    assert load_url_states_snapshot(path) is None


# ---------------------------------------------------------------------------
# run() end-to-end cohort wiring (#2687)
# ---------------------------------------------------------------------------

def _not_indexed(url: str, raw_state: str) -> dict:
    return {"url": url, "coverage_state": raw_state}


def test_run_first_call_writes_snapshot_and_cohort_previous_date_none(tmp_path, census_fixture):
    census_fixture["not_indexed_urls"] = [
        _not_indexed("https://navi.omcha.jp/products/b0000000a1/", "URL が Google に認識されていません"),
    ]
    appended, target_date = run(census_fixture, tmp_path, tmp_path / "articles")
    assert appended is True
    rows = _read_jsonl(tmp_path / CENSUS_HISTORY_FILE)
    assert rows[0]["cohort"] == {"previous_date": None}

    snapshot = json.loads((tmp_path / CENSUS_URL_STATES_FILE).read_text(encoding="utf-8"))
    assert snapshot["date"] == "2026-07-19"
    assert snapshot["states"] == {
        "https://navi.omcha.jp/products/b0000000a1/": "unknown_to_google",
    }


def test_run_second_call_computes_transitions_against_snapshot(tmp_path, census_fixture):
    articles_dir = tmp_path / "articles"
    articles_dir.mkdir()

    census1 = json.loads(json.dumps(census_fixture))
    census1["not_indexed_urls"] = [
        _not_indexed("https://navi.omcha.jp/products/b0000000a1/", "URL が Google に認識されていません"),
        _not_indexed("https://navi.omcha.jp/products/b0000000a2/", "URL が Google に認識されていません"),
    ]
    run(census1, tmp_path, articles_dir)

    # a1 exits and the article now exists (indexed); a2 progresses downstream
    (articles_dir / "2026-07-26-B0000000A1.json").write_text("{}", encoding="utf-8")

    census2 = json.loads(json.dumps(census_fixture))
    census2["fetched_at"] = "2026-07-26T22:57:54.878408+00:00"
    census2["not_indexed_urls"] = [
        _not_indexed("https://navi.omcha.jp/products/b0000000a2/", "検出 - インデックス未登録"),
    ]
    run(census2, tmp_path, articles_dir)

    rows = _read_jsonl(tmp_path / CENSUS_HISTORY_FILE)
    row2 = next(r for r in rows if r["date"] == "2026-07-26")
    cohort = row2["cohort"]
    assert cohort["previous_date"] == "2026-07-19"
    assert cohort["previous_not_indexed"] == 2
    assert cohort["transitions"] == {"unknown_to_google->discovered_not_indexed": 1}
    assert cohort["exited_not_indexed"] == 1
    assert cohort["exited_indexed"] == 1
    assert cohort["exited_dropped"] == 0
    assert cohort["entered_not_indexed"] == 0


def test_run_idempotent_rerun_does_not_touch_snapshot_or_jsonl(tmp_path, census_fixture):
    articles_dir = tmp_path / "articles"
    census_fixture["not_indexed_urls"] = [
        _not_indexed("https://navi.omcha.jp/products/b0000000a1/", "URL が Google に認識されていません"),
    ]
    run(census_fixture, tmp_path, articles_dir)
    snapshot_before = (tmp_path / CENSUS_URL_STATES_FILE).read_text(encoding="utf-8")
    jsonl_before = (tmp_path / CENSUS_HISTORY_FILE).read_text(encoding="utf-8")

    # Same date, but with different (bogus) not_indexed_urls content — if idempotency
    # were broken this would silently corrupt the snapshot used for next week's diff.
    census_dup = json.loads(json.dumps(census_fixture))
    census_dup["not_indexed_urls"] = [
        _not_indexed("https://navi.omcha.jp/products/b0000000zz/", "検出 - インデックス未登録"),
    ]
    appended, target_date = run(census_dup, tmp_path, articles_dir)

    assert appended is False
    assert target_date == "2026-07-19"
    assert (tmp_path / CENSUS_URL_STATES_FILE).read_text(encoding="utf-8") == snapshot_before
    assert (tmp_path / CENSUS_HISTORY_FILE).read_text(encoding="utf-8") == jsonl_before


def test_build_row_carries_rich_result_columns():
    """#5085: リッチリザルトの失効を時系列で検出できること。"""
    import append_census_history as A
    row = A.build_row({
        "fetched_at": "2026-08-20T00:00:00+00:00",
        "totals": {"sitemap_urls": 10, "inspected": 10, "indexed": 9,
                   "not_indexed": 1, "errors": 0},
        "by_coverage_state": {},
        "by_rich_verdict": {"PASS": 7, "PARTIAL": 2, "(none)": 1},
        "rich_issues": {"Product snippets / WARNING: x": 2},
    })
    assert row["rich_pass"] == 7
    assert row["rich_other"] == 2
    assert row["rich_none"] == 1
    assert row["rich_issue_kinds"] == 1


def test_build_row_rich_columns_default_to_zero_for_old_census():
    """リッチリザルト導入前の census を読んでも列は落ちない。"""
    import append_census_history as A
    row = A.build_row({
        "fetched_at": "2026-08-20T00:00:00+00:00",
        "totals": {"sitemap_urls": 10, "inspected": 10, "indexed": 9,
                   "not_indexed": 1, "errors": 0},
        "by_coverage_state": {},
    })
    assert row["rich_pass"] == 0
    assert row["rich_other"] == 0
    assert row["rich_none"] == 0
    assert row["rich_issue_kinds"] == 0
