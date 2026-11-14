"""Tests for scripts/append_ranking_history.py (Issue #600)."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from append_ranking_history import MAX_HISTORY, append_history  # noqa: E402


def _write_weekly(path: pathlib.Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_first_run_creates_history(tmp_path: pathlib.Path) -> None:
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    _write_weekly(weekly, [
        {"rank": 1, "matched_asin": "B0AAA", "title": "Item A"},
        {"rank": 2, "matched_asin": "B0BBB", "title": "Item B"},
        {"rank": 3, "matched_asin": None, "itemCode": "shop:3", "title": "Unmatched"},
    ])

    stats = append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T00:00:00+09:00")

    assert {k: stats[k] for k in ("matched", "skipped", "total_items", "tracked_asins", "today")} == {
        "matched": 2,
        "skipped": 1,
        "total_items": 3,
        "tracked_asins": 2,
        "today": "05-28",
    }
    state = json.loads(history.read_text(encoding="utf-8"))
    assert state["updated_at"] == "2026-05-28T00:00:00+09:00"
    assert state["history"]["B0AAA"] == {
        "name": "Item A",
        "history": [{"d": "05-28", "r": 1}],
    }
    assert state["history"]["B0BBB"]["history"] == [{"d": "05-28", "r": 2}]
    assert state["by_item"]["shop:3"]["history"] == [{"d": "05-28", "r": 3}]
    assert "by_item" not in state["history"]["B0AAA"]


def test_second_day_appends(tmp_path: pathlib.Path) -> None:
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    _write_weekly(weekly, [{"rank": 5, "matched_asin": "B0AAA", "title": "A"}])
    append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T00:00:00+09:00")

    _write_weekly(weekly, [{"rank": 3, "matched_asin": "B0AAA", "title": "A v2"}])
    append_history(weekly, history, today_mm_dd="05-29", now_iso="2026-05-29T00:00:00+09:00")

    state = json.loads(history.read_text(encoding="utf-8"))
    assert state["history"]["B0AAA"]["history"] == [
        {"d": "05-28", "r": 5},
        {"d": "05-29", "r": 3},
    ]
    assert state["history"]["B0AAA"]["name"] == "A v2"


def test_same_day_rerun_overwrites(tmp_path: pathlib.Path) -> None:
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    _write_weekly(weekly, [{"rank": 5, "matched_asin": "B0AAA", "title": "A"}])
    append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T08:00:00+09:00")

    _write_weekly(weekly, [{"rank": 2, "matched_asin": "B0AAA", "title": "A"}])
    append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T20:00:00+09:00")

    state = json.loads(history.read_text(encoding="utf-8"))
    entries = state["history"]["B0AAA"]["history"]
    assert len(entries) == 1
    assert entries[0] == {"d": "05-28", "r": 2}
    assert state["updated_at"] == "2026-05-28T20:00:00+09:00"


def test_rotation_at_90_entries(tmp_path: pathlib.Path) -> None:
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    # Seed with 90 existing days.
    seed = {
        "updated_at": None,
        "history": {
            "B0AAA": {
                "name": "A",
                "history": [{"d": f"d{i:03d}", "r": i} for i in range(90)],
            }
        },
    }
    history.write_text(json.dumps(seed), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 7, "matched_asin": "B0AAA", "title": "A"}])

    append_history(weekly, history, today_mm_dd="06-01", now_iso="2026-06-01T00:00:00+09:00")

    state = json.loads(history.read_text(encoding="utf-8"))
    entries = state["history"]["B0AAA"]["history"]
    assert len(entries) == MAX_HISTORY == 90
    # Oldest popped (d000), newest appended (06-01).
    assert entries[0]["d"] == "d001"
    assert entries[-1] == {"d": "06-01", "r": 7}


def test_unmatched_only_writes_empty_history(tmp_path: pathlib.Path) -> None:
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    _write_weekly(weekly, [{"rank": 1, "matched_asin": None, "title": "x"}])
    stats = append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T00:00:00+09:00")
    assert stats["matched"] == 0
    state = json.loads(history.read_text(encoding="utf-8"))
    assert state["history"] == {}
    assert state["updated_at"] == "2026-05-28T00:00:00+09:00"


def test_missing_weekly_raises(tmp_path: pathlib.Path) -> None:
    history = tmp_path / "history.json"
    with pytest.raises(FileNotFoundError):
        append_history(tmp_path / "missing.json", history)


def test_returning_asin_does_not_overwrite_last_years_same_day(tmp_path: pathlib.Path) -> None:
    """ランキングから外れて 1 年後の同じ MM-DD に戻ってきた ASIN は、古い entry を
    上書きせず末尾に追記する (時系列を壊さない)。"""
    weekly = tmp_path / "weekly.json"
    history = tmp_path / "history.json"
    history.write_text(json.dumps({
        "updated_at": "2025-11-03T00:00:00+09:00",
        "history": {"B0AAA": {"name": "A", "history": [
            {"d": "11-02", "r": 7},
            {"d": "11-03", "r": 9},
        ]}},
    }), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 4, "matched_asin": "B0AAA", "title": "A"}])

    append_history(weekly, history, today_mm_dd="11-02", now_iso="2026-11-02T00:00:00+09:00")

    state = json.loads(history.read_text(encoding="utf-8"))
    assert state["history"]["B0AAA"]["history"] == [
        {"d": "11-02", "r": 7},
        {"d": "11-03", "r": 9},
        {"d": "11-02", "r": 4},
    ]


def test_duplicate_asin_items_are_separate_and_movement(tmp_path: pathlib.Path) -> None:
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    history.write_text(json.dumps({"history": {}, "by_item": {
        "x": {"name": "X", "history": [{"d": "05-27", "r": 5}, {"d": "05-28", "r": 4}]},
        "y": {"name": "Y", "history": [{"d": "05-27", "r": 8}]},
    }}), encoding="utf-8")
    _write_weekly(weekly, [
        {"rank": 2, "matched_asin": "SAME", "itemCode": "x", "title": "X"},
        {"rank": 9, "matched_asin": "SAME", "itemCode": "y", "title": "Y"},
    ])
    append_history(weekly, history, movement_path=movement, today_mm_dd="05-29", now_iso="2026-05-29T00:00:00+09:00")
    state = json.loads(history.read_text(encoding="utf-8"))
    assert len(state["by_item"]["x"]["history"]) == 3
    assert len(state["by_item"]["y"]["history"]) == 2
    # ASIN shape and same-day overwrite semantics stay intact.
    assert state["history"]["SAME"]["history"] == [{"d": "05-29", "r": 9}]
    data = json.loads(movement.read_text(encoding="utf-8"))["items"]
    assert data["x"] == {"rank": 2, "prev_rank": 4, "prev_d": "05-28", "delta": 2, "is_new": False, "streak_days": 3}
    assert data["y"]["is_new"] is False and data["y"]["streak_days"] == 1


def test_year_boundary_movement(tmp_path: pathlib.Path) -> None:
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    history.write_text(json.dumps({"history": {}, "by_item": {"x": {"name": "X", "history": [
        {"d": "12-31", "r": 6}, {"d": "01-01", "r": 4}]}}}), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 3, "itemCode": "x", "title": "X"}])
    append_history(weekly, history, movement_path=movement, today_mm_dd="01-02", now_iso="2026-01-02T00:00:00+09:00")
    row = json.loads(movement.read_text(encoding="utf-8"))["items"]["x"]
    assert row["prev_d"] == "01-01" and row["delta"] == 1 and row["streak_days"] == 3


def test_first_run_suppresses_new_flood_and_rotates_by_item(tmp_path: pathlib.Path) -> None:
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    history.write_text(json.dumps({"history": {}, "by_item": {"x": {"name": "X", "history": [
        {"d": ( __import__("datetime").date(2026, 3, 3) + __import__("datetime").timedelta(days=i)).strftime("%m-%d"), "r": i}
        for i in range(MAX_HISTORY)]}}}), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 1, "itemCode": "x", "title": "X"}])
    append_history(weekly, history, movement_path=movement, today_mm_dd="06-01", now_iso="2026-06-01T00:00:00+09:00")
    entries = json.loads(history.read_text(encoding="utf-8"))["by_item"]["x"]["history"]
    assert len(entries) == MAX_HISTORY and entries[0]["d"] == "03-04"
    # Missing by_item marks the actual first run and suppresses is_new.
    history.unlink()
    append_history(weekly, history, movement_path=movement, today_mm_dd="06-01", now_iso="2026-06-01T00:00:00+09:00")
    assert json.loads(movement.read_text(encoding="utf-8"))["items"]["x"]["is_new"] is False


def test_same_day_rerun_after_first_run_is_not_a_new_flood(tmp_path: pathlib.Path) -> None:
    # 初回の記録と同じ日にもう一度走っても、前の日の点が無いうちは「初登場」を出さない。
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    _write_weekly(weekly, [{"rank": 1, "itemCode": "x", "title": "X"}, {"rank": 2, "itemCode": "y", "title": "Y"}])
    for _ in range(2):
        append_history(weekly, history, movement_path=movement, today_mm_dd="06-01", now_iso="2026-06-01T09:00:00+09:00")
    items = json.loads(movement.read_text(encoding="utf-8"))["items"]
    assert items["x"]["is_new"] is False and items["y"]["is_new"] is False
    # 翌日に初めて入った商品は初登場になる。
    _write_weekly(weekly, [{"rank": 1, "itemCode": "x", "title": "X"}, {"rank": 2, "itemCode": "z", "title": "Z"}])
    append_history(weekly, history, movement_path=movement, today_mm_dd="06-02", now_iso="2026-06-02T09:00:00+09:00")
    items = json.loads(movement.read_text(encoding="utf-8"))["items"]
    assert items["z"]["is_new"] is True and items["x"]["is_new"] is False


def test_point_from_almost_a_year_ago_is_not_recent(tmp_path: pathlib.Path) -> None:
    # 去年の 11-10 に一度だけ入った商品が今年の 11-14 に戻ってきた → 30 日以内ではないので初登場。
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    history.write_text(json.dumps({"history": {}, "by_item": {
        "old": {"name": "O", "history": [{"d": "11-10", "r": 7}, {"d": "12-01", "r": 9}]},
        "other": {"name": "P", "history": [{"d": "11-13", "r": 3}]},
    }}), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 5, "itemCode": "old", "title": "O"}])
    append_history(weekly, history, movement_path=movement, today_mm_dd="11-14", now_iso="2026-11-14T09:00:00+09:00")
    row = json.loads(movement.read_text(encoding="utf-8"))["items"]["old"]
    assert row["is_new"] is True and row["prev_d"] == "12-01" and row["streak_days"] == 1


def test_leap_day_point_does_not_crash(tmp_path: pathlib.Path) -> None:
    weekly, history, movement = tmp_path / "weekly.json", tmp_path / "history.json", tmp_path / "movement.json"
    history.write_text(json.dumps({"history": {}, "by_item": {
        "x": {"name": "X", "history": [{"d": "02-29", "r": 4}]}}}), encoding="utf-8")
    _write_weekly(weekly, [{"rank": 2, "itemCode": "x", "title": "X"}])
    append_history(weekly, history, movement_path=movement, today_mm_dd="03-01", now_iso="2026-03-01T09:00:00+09:00")
    row = json.loads(movement.read_text(encoding="utf-8"))["items"]["x"]
    # 2026 は平年なので 02-29 は 2024 年の点。前回順位は残るが 30 日以内ではない。
    assert row["prev_rank"] == 4 and row["is_new"] is True
