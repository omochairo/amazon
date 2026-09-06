"""scripts/check_pages_size.py unit tests (#6415)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from scripts.check_pages_size import (
    ApiError,
    MARKER,
    PAGES_LIMIT_BYTES,
    classify,
    latest_measurement,
    parse_size_from_trace,
    render_body,
    title_for,
)

GIB = 1024 ** 3


# --- parse_size_from_trace ------------------------------------------------

def test_parse_size_reads_the_value_line():
    trace = "some log\nPAGES_ARTIFACT_BYTES=698123456\nJob succeeded\n"
    assert parse_size_from_trace(trace) == 698123456


def test_parse_size_takes_the_last_match_not_the_command_echo():
    """GitLab の trace にはコマンド自体のエコーも残る。値は常に後ろに出る。"""
    trace = (
        '$ echo "PAGES_ARTIFACT_BYTES=${PAGES_ARTIFACT_BYTES}"\n'
        "PAGES_ARTIFACT_BYTES=123\n"
        "PAGES_ARTIFACT_BYTES=456\n"
    )
    assert parse_size_from_trace(trace) == 456


@pytest.mark.parametrize("trace", ["", None, "no size here", "PAGES_ARTIFACT_BYTES=abc"])
def test_parse_size_returns_none_when_absent(trace):
    assert parse_size_from_trace(trace) is None


# --- classify --------------------------------------------------------------

@pytest.mark.parametrize("ratio,expected", [
    (0.10, "ok"),
    (0.79, "ok"),
    (0.80, "warn"),     # 境界は含む
    (0.89, "warn"),
    (0.90, "alert"),    # 境界は含む
    (1.20, "alert"),
])
def test_classify_thresholds(ratio, expected):
    # 境界は「その割合に達したバイト数」で見る。int() で切り捨てると
    # 0.90 * GiB が 0.8999... になり、境界の検査にならない
    assert classify(math.ceil(ratio * GIB)) == expected


def test_classify_does_not_fire_at_the_size_when_the_monitor_was_written():
    """導入時点 (2026-08-31 実測 644,608 KB) では鳴らない = 入れた日に赤くならない。"""
    assert classify(644608 * 1024) == "ok"


def test_classify_fires_at_the_size_that_broke_production():
    """2026-08-28 に本番を 19 時間止めた 1,059,523,450 bytes では必ず鳴る。"""
    assert classify(1059523450) == "alert"


# --- latest_measurement ----------------------------------------------------

def _fetcher(jobs, traces):
    def fetch(path, raw=False):
        if "/trace" in path:
            job_id = int(path.rsplit("/", 2)[-2])
            return traces.get(job_id, "")
        return jobs
    return fetch


def test_latest_measurement_reads_the_newest_pages_job():
    jobs = [
        {"id": 2, "name": "cf-purge", "finished_at": "2026-09-06T12:00:00Z"},
        {"id": 1, "name": "pages", "finished_at": "2026-09-06T11:00:00Z",
         "commit": {"id": "abc123def456789"}},
    ]
    got = latest_measurement("p", "t", fetch=_fetcher(jobs, {1: "PAGES_ARTIFACT_BYTES=42"}))
    assert got["bytes"] == 42
    assert got["job_id"] == 1
    assert got["sha"] == "abc123def456789"


def test_latest_measurement_skips_jobs_without_a_size_line():
    """計測を入れる前のジョブは飛ばす。**0 と混ぜない** (#5941 と同じ型の穴)。"""
    jobs = [
        {"id": 9, "name": "pages", "finished_at": "2026-09-06T12:00:00Z"},
        {"id": 8, "name": "pages", "finished_at": "2026-09-05T12:00:00Z"},
    ]
    traces = {9: "old job, no measurement", 8: "PAGES_ARTIFACT_BYTES=777"}
    got = latest_measurement("p", "t", fetch=_fetcher(jobs, traces))
    assert got["job_id"] == 8
    assert got["bytes"] == 777


def test_latest_measurement_returns_none_when_nothing_measured():
    jobs = [{"id": 9, "name": "pages"}]
    assert latest_measurement("p", "t", fetch=_fetcher(jobs, {9: "nothing"})) is None


def test_latest_measurement_ignores_other_job_names():
    jobs = [{"id": 3, "name": "deploy-nas"}]
    assert latest_measurement("p", "t", fetch=_fetcher(jobs, {3: "PAGES_ARTIFACT_BYTES=1"})) is None


def test_latest_measurement_raises_when_jobs_api_shape_is_wrong():
    with pytest.raises(ApiError):
        latest_measurement("p", "t", fetch=lambda path, raw=False: {"message": "401"})


# --- render_body / title ---------------------------------------------------

def test_render_body_carries_the_marker_comment():
    """マーカーが本文から消えると upsert が同じ issue を見つけられなくなる。"""
    body = render_body({"bytes": int(0.85 * GIB), "job_id": 1,
                        "finished_at": "2026-09-06T00:00:00Z", "sha": "a" * 40},
                       "warn", now=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
    assert "<!-- {} -->".format(MARKER) in body
    assert body.startswith("<!-- {} -->".format(MARKER))


def test_render_body_states_that_production_does_not_stop():
    """「本番が落ちる」と読み違えると優先度を誤る。効くのは待機系に倒したとき。"""
    body = render_body({"bytes": int(0.85 * GIB), "job_id": 1,
                        "finished_at": None, "sha": None}, "warn")
    assert "本番 (NAS) はこれでは止まりません" in body


def test_render_body_handles_missing_job_metadata():
    body = render_body({"bytes": 1, "job_id": None, "finished_at": None, "sha": None}, "ok")
    assert "| commit | `-` |" in body


def test_title_shows_percentage_and_size():
    title = title_for(int(0.85 * GIB), PAGES_LIMIT_BYTES)
    assert "85%" in title
    assert "MiB" in title
