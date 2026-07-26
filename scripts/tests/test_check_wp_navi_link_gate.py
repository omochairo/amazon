"""scripts/check_wp_navi_link_gate.py unit tests (#3988 C-1 / 対象 #3333)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.check_wp_navi_link_gate import (
    DEFAULT_CENTROID_MAX,
    DEFAULT_MIN_POINTS,
    DEFAULT_TREND_WINDOW,
    compute_gate,
    evaluate_c1,
    evaluate_c2,
    filter_usable_census_rows,
    load_census_rows,
    load_uniqueness,
    main,
    resolve_verdict,
    select_trend_window,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _uniqueness(centroid_sim_p50, *, with_all=True):
    payload = {
        "generated_at": "2026-07-26T00:00:00Z",
        "corpus_size": 1602,
        "cohort_stats": {
            "pre_v7": {"count": 100, "max_sim_p50": 0.94, "centroid_sim_p50": 0.93},
            "post_v7": {"count": 1502, "max_sim_p50": 0.90, "centroid_sim_p50": 0.86},
        },
    }
    if with_all:
        payload["cohort_stats"]["all"] = {
            "count": 1602, "max_sim_p50": 0.91, "centroid_sim_p50": centroid_sim_p50,
        }
    return payload


def _census_row(date, crawled_not_indexed, *, tripped=False, indexed=1000, inspected=1500,
                 indexed_rate=0.6667, not_found_404=50):
    return {
        "date": date,
        "indexed": indexed,
        "inspected": inspected,
        "indexed_rate": indexed_rate,
        "not_found_404": not_found_404,
        "crawled_not_indexed": crawled_not_indexed,
        "circuit_breaker_tripped": tripped,
    }


DECREASING_ROWS = [
    _census_row("2026-07-05", 120),
    _census_row("2026-07-12", 100),
    _census_row("2026-07-19", 90),
]

FLAT_ROWS = [
    _census_row("2026-07-05", 90),
    _census_row("2026-07-12", 95),
    _census_row("2026-07-19", 90),
]


# ---------------------------------------------------------------------------
# C1 (corpus de-genericization)
# ---------------------------------------------------------------------------

def test_c1_pass_below_threshold():
    r = evaluate_c1(_uniqueness(0.80), centroid_max=0.88)
    assert r["status"] == "pass"
    assert r["actual"] == 0.80


def test_c1_fail_above_threshold():
    r = evaluate_c1(_uniqueness(0.90), centroid_max=0.88)
    assert r["status"] == "fail"


def test_c1_boundary_equal_threshold_is_fail():
    """centroid_sim_p50 が閾値ちょうどのときは strictly '<' なので fail。"""
    r = evaluate_c1(_uniqueness(0.88), centroid_max=0.88)
    assert r["status"] == "fail"


def test_c1_old_schema_missing_all_is_unknown():
    """cohort_stats に pre_v7/post_v7 のみで all が無い旧スキーマ → unknown。

    pre_v7/post_v7 の中央値へは絶対にフォールバックしない。
    """
    data = _uniqueness(0.5, with_all=False)
    assert "all" not in data["cohort_stats"]
    r = evaluate_c1(data)
    assert r["status"] == "unknown"
    assert r["actual"] is None


def test_c1_all_present_but_centroid_none_is_unknown():
    data = _uniqueness(0.5)
    data["cohort_stats"]["all"]["centroid_sim_p50"] = None
    r = evaluate_c1(data)
    assert r["status"] == "unknown"


def test_c1_missing_uniqueness_data_is_unknown():
    r = evaluate_c1(None)
    assert r["status"] == "unknown"


def test_c1_default_threshold_is_0_88():
    assert DEFAULT_CENTROID_MAX == 0.88


# ---------------------------------------------------------------------------
# C2 (crawl-quality trend)
# ---------------------------------------------------------------------------

def test_c2_pass_net_decrease():
    r = evaluate_c2(DECREASING_ROWS, trend_window=3, min_points=3)
    assert r["status"] == "pass"
    assert r["actual"]["first_value"] == 120
    assert r["actual"]["last_value"] == 90


def test_c2_fail_flat_or_rising():
    r = evaluate_c2(FLAT_ROWS, trend_window=3, min_points=3)
    assert r["status"] == "fail"


def test_c2_fail_rising():
    rows = [
        _census_row("2026-07-05", 80),
        _census_row("2026-07-12", 90),
        _census_row("2026-07-19", 100),
    ]
    r = evaluate_c2(rows, trend_window=3, min_points=3)
    assert r["status"] == "fail"


def test_c2_missing_file_yields_no_rows_and_unknown():
    r = evaluate_c2([], trend_window=3, min_points=3)
    assert r["status"] == "unknown"


def test_c2_fewer_than_min_points_is_unknown():
    rows = [_census_row("2026-07-12", 100), _census_row("2026-07-19", 90)]
    r = evaluate_c2(rows, trend_window=3, min_points=3)
    assert r["status"] == "unknown"


def test_c2_circuit_breaker_rows_excluded_from_trend():
    """circuit_breaker_tripped な行は使用可能行数にもウィンドウにも入らない。"""
    rows = [
        _census_row("2026-07-05", 999, tripped=True),  # 除外されるべき
        _census_row("2026-07-12", 100),
        _census_row("2026-07-19", 90),
    ]
    usable = filter_usable_census_rows(rows)
    assert len(usable) == 2
    r = evaluate_c2(rows, trend_window=3, min_points=2)
    assert r["status"] == "pass"
    assert r["actual"]["first_value"] == 100
    assert r["actual"]["last_value"] == 90


def test_c2_excluding_tripped_rows_drops_below_min_points_is_unknown():
    rows = [
        _census_row("2026-07-05", 999, tripped=True),
        _census_row("2026-07-12", 100),
        _census_row("2026-07-19", 90),
    ]
    # min_points=3 だが tripped 除外後は 2 件しか残らない
    r = evaluate_c2(rows, trend_window=3, min_points=3)
    assert r["status"] == "unknown"


def test_c2_window_takes_last_n_sorted_by_date_ascending():
    rows = [
        _census_row("2026-06-28", 200),
        _census_row("2026-07-05", 120),
        _census_row("2026-07-12", 100),
        _census_row("2026-07-19", 90),
    ]
    # unsorted 入力でも date 昇順に並べ替えて末尾3件を使う
    import random
    shuffled = rows[:]
    random.Random(0).shuffle(shuffled)
    r = evaluate_c2(shuffled, trend_window=3, min_points=3)
    assert r["actual"]["first_value"] == 120  # 2026-07-05 (末尾3件の先頭)
    assert r["actual"]["last_value"] == 90


def test_select_trend_window_basic():
    rows = [
        _census_row("2026-07-19", 90),
        _census_row("2026-07-05", 120),
        _census_row("2026-07-12", 100),
    ]
    window = select_trend_window(rows, 2)
    assert [r["date"] for r in window] == ["2026-07-12", "2026-07-19"]


def test_c2_default_windows():
    assert DEFAULT_TREND_WINDOW == 3
    assert DEFAULT_MIN_POINTS == 3


# ---------------------------------------------------------------------------
# verdict resolution (最重要: unknown は絶対に go に寄与しない)
# ---------------------------------------------------------------------------

def test_verdict_go_both_pass():
    assert resolve_verdict("pass", "pass") == "go"


def test_verdict_hold_c1_fail():
    assert resolve_verdict("fail", "pass") == "hold"


def test_verdict_hold_c2_fail():
    assert resolve_verdict("pass", "fail") == "hold"


def test_verdict_insufficient_data_c1_unknown():
    assert resolve_verdict("unknown", "pass") == "insufficient_data"


def test_verdict_insufficient_data_c2_unknown():
    assert resolve_verdict("pass", "unknown") == "insufficient_data"


def test_verdict_insufficient_data_both_unknown():
    assert resolve_verdict("unknown", "unknown") == "insufficient_data"


def test_verdict_fail_beats_unknown():
    """fail + unknown の組み合わせは hold (fail の方が unknown より優先)。"""
    assert resolve_verdict("fail", "unknown") == "hold"
    assert resolve_verdict("unknown", "fail") == "hold"


@pytest.mark.parametrize(
    "c1,c2",
    [
        ("unknown", "pass"), ("pass", "unknown"), ("unknown", "unknown"),
        ("unknown", "fail"), ("fail", "unknown"),
    ],
)
def test_verdict_never_go_when_any_unknown(c1, c2):
    """unknown が絡む組み合わせは絶対に go にならない (このスクリプトの核心)。"""
    assert resolve_verdict(c1, c2) != "go"


# ---------------------------------------------------------------------------
# compute_gate (統合)
# ---------------------------------------------------------------------------

def test_compute_gate_both_pass_is_go():
    result = compute_gate(_uniqueness(0.80), DECREASING_ROWS)
    assert result["verdict"] == "go"
    assert result["criteria"]["c1_corpus_degenericization"]["status"] == "pass"
    assert result["criteria"]["c2_crawl_quality_trend"]["status"] == "pass"


def test_compute_gate_c1_fail_is_hold():
    result = compute_gate(_uniqueness(0.90), DECREASING_ROWS)
    assert result["verdict"] == "hold"


def test_compute_gate_c2_fail_is_hold():
    result = compute_gate(_uniqueness(0.80), FLAT_ROWS)
    assert result["verdict"] == "hold"


def test_compute_gate_old_schema_uniqueness_is_insufficient_data_not_go():
    old_schema = _uniqueness(0.5, with_all=False)
    result = compute_gate(old_schema, DECREASING_ROWS)
    assert result["criteria"]["c1_corpus_degenericization"]["status"] == "unknown"
    assert result["verdict"] == "insufficient_data"
    assert result["verdict"] != "go"


def test_compute_gate_missing_census_history_is_insufficient_data_not_go():
    result = compute_gate(_uniqueness(0.80), [])
    assert result["criteria"]["c2_crawl_quality_trend"]["status"] == "unknown"
    assert result["verdict"] == "insufficient_data"
    assert result["verdict"] != "go"


def test_compute_gate_context_does_not_affect_verdict():
    """context に異常値が入っていても verdict の計算には使われない。"""
    result = compute_gate(_uniqueness(0.80), DECREASING_ROWS)
    ctx = result["context"]
    assert ctx["corpus_size"] == 1602
    assert ctx["centroid_sim_p50"] == 0.80
    assert ctx["census_date"] == "2026-07-19"
    assert ctx["indexed"] == 1000
    assert ctx["inspected"] == 1500
    assert ctx["not_found_404"] == 50
    # context を書き換えても verdict ロジックには影響しないことを別途 resolve_verdict で保証済み


# ---------------------------------------------------------------------------
# load_uniqueness / load_census_rows (IO)
# ---------------------------------------------------------------------------

def test_load_uniqueness_missing_file_returns_none(tmp_path):
    assert load_uniqueness(tmp_path / "nope.json") is None


def test_load_uniqueness_reads_json(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps(_uniqueness(0.5)), encoding="utf-8")
    data = load_uniqueness(path)
    assert data["corpus_size"] == 1602


def test_load_census_rows_missing_file_returns_empty(tmp_path):
    assert load_census_rows(tmp_path / "nope.jsonl") == []


def test_load_census_rows_reads_jsonl_and_skips_corrupt_lines(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(_census_row("2026-07-05", 120)) + "\n"
        "not json\n"
        + json.dumps(_census_row("2026-07-12", 100)) + "\n",
        encoding="utf-8",
    )
    rows = load_census_rows(path)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# main() / CLI exit codes
# ---------------------------------------------------------------------------

def _write_fixtures(tmp_path, uniqueness_data, census_rows):
    u_path = tmp_path / "uniqueness_audit.json"
    u_path.write_text(json.dumps(uniqueness_data), encoding="utf-8")
    c_path = tmp_path / "gsc_index_census.jsonl"
    c_path.write_text(
        "\n".join(json.dumps(r) for r in census_rows) + ("\n" if census_rows else ""),
        encoding="utf-8",
    )
    return u_path, c_path


def test_main_strict_exit_0_for_go(tmp_path, monkeypatch, capsys):
    u_path, c_path = _write_fixtures(tmp_path, _uniqueness(0.80), DECREASING_ROWS)
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path), "--strict"],
    )
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict: `go`" in out


def test_main_strict_exit_1_for_hold(tmp_path, monkeypatch, capsys):
    u_path, c_path = _write_fixtures(tmp_path, _uniqueness(0.95), DECREASING_ROWS)
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path), "--strict"],
    )
    rc = main()
    assert rc == 1


def test_main_strict_exit_1_for_insufficient_data(tmp_path, monkeypatch, capsys):
    old_schema = _uniqueness(0.5, with_all=False)
    u_path, c_path = _write_fixtures(tmp_path, old_schema, DECREASING_ROWS)
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path), "--strict"],
    )
    rc = main()
    assert rc == 1


@pytest.mark.parametrize(
    "centroid,rows",
    [
        (0.80, DECREASING_ROWS),  # go
        (0.95, DECREASING_ROWS),  # hold (C1 fail)
        (0.80, FLAT_ROWS),        # hold (C2 fail)
    ],
)
def test_main_default_non_strict_always_exit_0(tmp_path, monkeypatch, centroid, rows):
    u_path, c_path = _write_fixtures(tmp_path, _uniqueness(centroid), rows)
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path)],
    )
    assert main() == 0


def test_main_default_non_strict_exit_0_for_insufficient_data(tmp_path, monkeypatch):
    old_schema = _uniqueness(0.5, with_all=False)
    u_path, c_path = _write_fixtures(tmp_path, old_schema, DECREASING_ROWS)
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path)],
    )
    assert main() == 0


def test_main_writes_json_out(tmp_path, monkeypatch):
    u_path, c_path = _write_fixtures(tmp_path, _uniqueness(0.80), DECREASING_ROWS)
    json_out = tmp_path / "out" / "verdict.json"
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(c_path), "--json-out", str(json_out)],
    )
    main()
    written = json.loads(json_out.read_text(encoding="utf-8"))
    assert written["verdict"] == "go"


def test_main_missing_census_history_file_is_insufficient_data(tmp_path, monkeypatch, capsys):
    u_path = tmp_path / "uniqueness_audit.json"
    u_path.write_text(json.dumps(_uniqueness(0.80)), encoding="utf-8")
    missing_census = tmp_path / "does_not_exist.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["check_wp_navi_link_gate.py", "--uniqueness", str(u_path),
         "--census-history", str(missing_census)],
    )
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict: `insufficient_data`" in out
