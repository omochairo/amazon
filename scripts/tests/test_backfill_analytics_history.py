"""scripts/backfill_analytics_history.py unit tests (#2821 wave2 followup)."""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import date

import pytest

import scripts.backfill_analytics_history as backfill
from scripts.append_analytics_history import (
    GA4_PAGES_FILE,
    GA4_TOTALS_FILE,
    GSC_TOTALS_FILE,
)


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        property_id="123", sa_json="{}", top_n=100,
        site_url="https://navi.omcha.jp/", client_id="cid",
        client_secret="csecret", refresh_token="rtoken",
        sleep_seconds=0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_ga4(property_id, sa_json, days, top_n, end_date=None):
    return {
        "range": {"start": end_date, "end": end_date, "days": days},
        "totals": {"rows_by_page": 1, "screenPageViews_sum": 10, "engagedSessions_sum": 4},
        "by_page": [{"hostName": "navi.omcha.jp", "pagePath": "/x/",
                     "screenPageViews": 10, "engagedSessions": 4,
                     "averageSessionDuration": 1.0, "bounceRate": 0.5}],
    }


def _fake_gsc(site_url, client_id, client_secret, refresh_token, days, delay, end_date=None):
    return {
        "range": {"start": end_date, "end": end_date, "days": days, "delay_days": delay},
        "totals": {"queries": 1, "pages": 1, "clicks_sum": 2, "impressions_sum": 20},
        "by_query": [{"query": "q", "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 3.0}],
        "by_page": [{"page": "/x/", "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 3.0}],
        "by_combo": [],
    }


def test_date_range_inclusive():
    days = list(backfill._date_range(date(2026, 6, 24), date(2026, 6, 26)))
    assert days == [date(2026, 6, 24), date(2026, 6, 25), date(2026, 6, 26)]


def test_run_backfills_each_day(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "fetch_ga4", _fake_ga4)
    monkeypatch.setattr(backfill, "fetch_gsc", _fake_gsc)

    total = backfill.run(date(2026, 6, 24), date(2026, 6, 25), tmp_path, _args())

    ga4_totals = _read_jsonl(tmp_path / GA4_TOTALS_FILE)
    gsc_totals = _read_jsonl(tmp_path / GSC_TOTALS_FILE)
    assert {r["date"] for r in ga4_totals} == {"2026-06-24", "2026-06-25"}
    assert {r["date"] for r in gsc_totals} == {"2026-06-24", "2026-06-25"}
    assert total > 0


def test_run_idempotent_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "fetch_ga4", _fake_ga4)
    monkeypatch.setattr(backfill, "fetch_gsc", _fake_gsc)

    backfill.run(date(2026, 6, 24), date(2026, 6, 25), tmp_path, _args())
    second_total = backfill.run(date(2026, 6, 24), date(2026, 6, 25), tmp_path, _args())

    assert second_total == 0
    ga4_totals = _read_jsonl(tmp_path / GA4_TOTALS_FILE)
    assert len(ga4_totals) == 2  # 再実行で重複していない


def test_run_skips_ga4_without_credentials(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backfill, "fetch_ga4", lambda *a, **k: calls.append("ga4") or _fake_ga4(*a, **k))
    monkeypatch.setattr(backfill, "fetch_gsc", _fake_gsc)

    backfill.run(date(2026, 6, 24), date(2026, 6, 24), tmp_path,
                 _args(property_id=None, sa_json=None))

    assert calls == []
    assert not (tmp_path / GA4_TOTALS_FILE).exists()
    assert (tmp_path / GSC_TOTALS_FILE).exists()


def test_run_continues_after_one_day_failure(tmp_path, monkeypatch):
    def flaky_ga4(property_id, sa_json, days, top_n, end_date=None):
        if end_date == "2026-06-24":
            raise RuntimeError("simulated API failure")
        return _fake_ga4(property_id, sa_json, days, top_n, end_date)

    monkeypatch.setattr(backfill, "fetch_ga4", flaky_ga4)
    monkeypatch.setattr(backfill, "fetch_gsc", _fake_gsc)

    backfill.run(date(2026, 6, 24), date(2026, 6, 25), tmp_path, _args())

    ga4_totals = _read_jsonl(tmp_path / GA4_TOTALS_FILE)
    assert {r["date"] for r in ga4_totals} == {"2026-06-25"}  # 24日だけ欠けて25日は成功


def test_main_rejects_start_after_end(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_analytics_history.py", "--start-date", "2026-07-02", "--end-date", "2026-07-01"],
    )
    assert backfill.main() == 2
