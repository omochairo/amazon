"""scripts/article_format_log.py unit tests (#7954)."""
from __future__ import annotations

import json
import pathlib

from scripts.article_format_log import (
    append_rows,
    build_rows,
    compute_redated,
    earliest_dates_by_asin,
    is_first_of_month,
    load_asin_origin_pool,
    load_last_states,
)


class TestEarliestDatesByAsin:
    def test_single_file_per_asin(self):
        stems = ["2026-05-14-B0F2T9PFS9", "2026-05-15-B0899JMQ7F"]
        assert earliest_dates_by_asin(stems) == {
            "B0F2T9PFS9": "2026-05-14",
            "B0899JMQ7F": "2026-05-15",
        }

    def test_multiple_files_keeps_earliest(self):
        stems = ["2026-05-19-B00V9LDHJ0", "2026-09-22-B00V9LDHJ0"]
        assert earliest_dates_by_asin(stems) == {"B00V9LDHJ0": "2026-05-19"}

    def test_ignores_non_matching_stems(self):
        stems = ["2026-05-19-B00V9LDHJ0", "not-a-dated-file", "2026-13-99-BADDATE1"]
        # "2026-13-99-BADDATE1" ASIN part doesn't match B0.. pattern so it's ignored
        assert earliest_dates_by_asin(stems) == {"B00V9LDHJ0": "2026-05-19"}


class TestLoadAsinOriginPool:
    def test_missing_file_returns_empty(self, tmp_path: pathlib.Path):
        assert load_asin_origin_pool(tmp_path / "nope.jsonl") == {}

    def test_last_row_wins(self, tmp_path: pathlib.Path):
        p = tmp_path / "asin_origin.jsonl"
        p.write_text(
            '{"asin": "B0AAA00001", "pool": "rewrite-queue"}\n'
            '{"asin": "B0AAA00002", "pool": "new-demand"}\n'
            '{"asin": "B0AAA00001", "pool": "new-demand"}\n',
            encoding="utf-8",
        )
        assert load_asin_origin_pool(p) == {
            "B0AAA00001": "new-demand",
            "B0AAA00002": "new-demand",
        }

    def test_ignores_malformed_lines(self, tmp_path: pathlib.Path):
        p = tmp_path / "asin_origin.jsonl"
        p.write_text('not json\n{"asin": "B0AAA00001", "pool": "rewrite-queue"}\n', encoding="utf-8")
        assert load_asin_origin_pool(p) == {"B0AAA00001": "rewrite-queue"}


class TestComputeRedated:
    def test_rewrite_queue_pool_is_redated(self):
        assert compute_redated("B0X", "2026-09-01", "rewrite-queue", None) is True

    def test_older_filename_date_is_redated(self):
        assert compute_redated("B0X", "2026-09-22", None, "2026-05-19") is True

    def test_no_signal_is_not_redated(self):
        assert compute_redated("B0X", "2026-09-22", "new-demand", None) is False
        assert compute_redated("B0X", "2026-09-22", None, None) is False

    def test_earliest_date_equal_to_article_date_is_not_redated(self):
        # 同日 = 今回のファイル自身 (書き直しの兆候ではない)
        assert compute_redated("B0X", "2026-09-22", None, "2026-09-22") is False

    def test_earliest_date_after_article_date_is_not_redated(self):
        assert compute_redated("B0X", "2026-05-19", None, "2026-09-22") is False

    def test_iso_timestamp_article_date_same_day_is_not_redated(self):
        # #7954 回帰: article_date が "YYYY-MM-DDTHH:MM:SS+09:00" 形式の場合、
        # 素の文字列比較だと同日でも "2026-05-14" < "2026-05-14T10:00:00+09:00"
        # が真になってしまい誤って redated=True になるバグがあった。
        assert compute_redated("B0X", "2026-05-14T10:00:00+09:00", None, "2026-05-14") is False

    def test_iso_timestamp_article_date_earlier_filename_is_redated(self):
        assert compute_redated("B0X", "2026-09-22T10:00:00+09:00", None, "2026-05-14") is True


class TestLoadLastStates:
    def test_missing_file_returns_empty(self, tmp_path: pathlib.Path):
        assert load_last_states(tmp_path / "nope.jsonl") == {}

    def test_last_occurrence_wins(self, tmp_path: pathlib.Path):
        p = tmp_path / "article_format.jsonl"
        p.write_text(
            json.dumps({"date": "2026-09-01", "asin": "B0X", "format": "legacy",
                        "official_howto": "none", "redated": False}) + "\n"
            + json.dumps({"date": "2026-09-15", "asin": "B0X", "format": "stock",
                          "official_howto": "steps", "redated": False}) + "\n",
            encoding="utf-8",
        )
        assert load_last_states(p) == {
            "B0X": {"format": "stock", "official_howto": "steps", "redated": False},
        }


class TestIsFirstOfMonth:
    def test_no_file_is_first(self, tmp_path: pathlib.Path):
        assert is_first_of_month(tmp_path / "nope.jsonl", "2026-09-22") is True

    def test_same_month_row_exists(self, tmp_path: pathlib.Path):
        p = tmp_path / "article_format.jsonl"
        p.write_text(json.dumps({"date": "2026-09-01", "asin": "B0X"}) + "\n", encoding="utf-8")
        assert is_first_of_month(p, "2026-09-22") is False

    def test_different_month_is_first(self, tmp_path: pathlib.Path):
        p = tmp_path / "article_format.jsonl"
        p.write_text(json.dumps({"date": "2026-08-01", "asin": "B0X"}) + "\n", encoding="utf-8")
        assert is_first_of_month(p, "2026-09-22") is True


class TestBuildRows:
    def test_first_run_writes_all_as_new(self):
        current = {
            "B0A": {"format": "stock", "official_howto": "steps", "redated": False},
            "B0B": {"format": "legacy", "official_howto": "none", "redated": True},
        }
        rows = build_rows(current, {}, "2026-09-22", snapshot=False)
        assert rows == [
            {"date": "2026-09-22", "asin": "B0A", "format": "stock",
             "official_howto": "steps", "redated": False},
            {"date": "2026-09-22", "asin": "B0B", "format": "legacy",
             "official_howto": "none", "redated": True},
        ]

    def test_unchanged_state_produces_no_row(self):
        current = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        previous = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        assert build_rows(current, previous, "2026-09-22", snapshot=False) == []

    def test_changed_field_produces_row(self):
        current = {"B0A": {"format": "stock", "official_howto": "link", "redated": False}}
        previous = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        rows = build_rows(current, previous, "2026-09-22", snapshot=False)
        assert rows == [
            {"date": "2026-09-22", "asin": "B0A", "format": "stock",
             "official_howto": "link", "redated": False},
        ]

    def test_asin_missing_from_current_run_produces_no_row(self):
        # #7954: census と違い「退場」に意味のある終端状態を割り当てない。
        current: dict = {}
        previous = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        assert build_rows(current, previous, "2026-09-22", snapshot=False) == []

    def test_monthly_snapshot_writes_all_regardless_of_change(self):
        current = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        previous = {"B0A": {"format": "stock", "official_howto": "steps", "redated": False}}
        rows = build_rows(current, previous, "2026-09-01", snapshot=True)
        assert rows == [
            {"date": "2026-09-01", "asin": "B0A", "format": "stock",
             "official_howto": "steps", "redated": False, "snapshot": True},
        ]

    def test_rows_sorted_by_asin(self):
        current = {
            "B0Z": {"format": "legacy", "official_howto": "none", "redated": False},
            "B0A": {"format": "legacy", "official_howto": "none", "redated": False},
        }
        rows = build_rows(current, {}, "2026-09-22", snapshot=False)
        assert [r["asin"] for r in rows] == ["B0A", "B0Z"]


class TestAppendRows:
    def test_noop_on_empty_rows(self, tmp_path: pathlib.Path):
        p = tmp_path / "sub" / "article_format.jsonl"
        assert append_rows(p, []) == 0
        assert not p.exists()

    def test_appends_and_creates_parent_dir(self, tmp_path: pathlib.Path):
        p = tmp_path / "sub" / "article_format.jsonl"
        rows = [{"date": "2026-09-22", "asin": "B0A", "format": "stock",
                 "official_howto": "steps", "redated": False}]
        assert append_rows(p, rows) == 1
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["asin"] == "B0A"

    def test_append_twice_accumulates(self, tmp_path: pathlib.Path):
        p = tmp_path / "article_format.jsonl"
        row1 = [{"date": "2026-09-01", "asin": "B0A", "format": "legacy",
                 "official_howto": "none", "redated": False}]
        row2 = [{"date": "2026-09-02", "asin": "B0A", "format": "stock",
                 "official_howto": "steps", "redated": False}]
        append_rows(p, row1)
        append_rows(p, row2)
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
