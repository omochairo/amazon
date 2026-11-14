"""ranking_dates.resolve_entry_dates: 年の無い MM-DD を挿入順で日付に戻す (navi-brain#166)。"""
from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ranking_dates import resolve_entry_dates  # noqa: E402


def _rows(*mm_dd: str) -> list:
    return [{"d": d, "r": 1} for d in mm_dd]


def test_recent_points_resolve_to_this_year() -> None:
    assert resolve_entry_dates(_rows("11-12", "11-13", "11-14"), date(2026, 11, 14)) == [
        date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)]


def test_year_boundary() -> None:
    assert resolve_entry_dates(_rows("12-31", "01-01"), date(2026, 1, 2)) == [date(2025, 12, 31), date(2026, 1, 1)]


def test_old_point_before_a_later_one_goes_to_last_year() -> None:
    # 11-10 は今日 (11-14) より前だが、後ろに 12-01 (= 去年) があるので去年の 11-10。
    assert resolve_entry_dates(_rows("11-10", "12-01", "11-13"), date(2026, 11, 14)) == [
        date(2025, 11, 10), date(2025, 12, 1), date(2026, 11, 13)]


def test_leap_day_and_bad_rows() -> None:
    rows = [{"d": "02-29"}, {"x": 1}, {"d": "13-40"}, {"d": "03-01"}]
    assert resolve_entry_dates(rows, date(2026, 3, 2)) == [date(2024, 2, 29), None, None, date(2026, 3, 1)]
