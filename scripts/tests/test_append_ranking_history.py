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
        {"rank": 3, "matched_asin": None, "title": "Unmatched"},
    ])

    stats = append_history(weekly, history, today_mm_dd="05-28", now_iso="2026-05-28T00:00:00+09:00")

    assert stats == {
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
