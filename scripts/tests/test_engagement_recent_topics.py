"""_engagement_recent_topics.py の単体テスト。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scripts._engagement_recent_topics import JST, parse_dt, scan_recent


def _write_queue(base, rows):
    p = base / "data" / "engagement_queue_x.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


def test_parse_dt_naive_is_treated_as_jst():
    got = parse_dt("2026-11-01T10:00:00")
    assert got == datetime(2026, 11, 1, 10, 0, tzinfo=JST)


def test_parse_dt_keeps_explicit_offset_and_z():
    assert parse_dt("2026-11-01T10:00:00Z") == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    assert parse_dt("2026-11-01T10:00:00+09:00") == datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    assert parse_dt("") is None
    assert parse_dt(None) is None
    assert parse_dt("not a date") is None


def test_scan_recent_does_not_crash_on_naive_timestamp(tmp_path):
    # 以前は naive と aware の比較で TypeError になり、集計全体が落ちていた
    now = datetime(2026, 11, 2, 12, 0, tzinfo=JST)
    _write_queue(tmp_path, [
        {"category": "news", "subcategory": "台風", "created_at": "2026-11-02T09:00:00"},
        {"category": "news", "subcategory": "休校", "created_at": "2026-11-02T09:00:00+09:00"},
        {"category": "news", "subcategory": "古い", "created_at": "2026-10-20T09:00:00"},
    ])
    rows = scan_recent(48, now, tmp_path)
    assert [r["subcategory"] for r in rows] == ["台風", "休校"]


def test_scan_recent_window_boundary(tmp_path):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=JST)
    edge = (now - timedelta(hours=48)).isoformat()
    _write_queue(tmp_path, [{"category": "news", "subcategory": "境界", "created_at": edge}])
    assert len(scan_recent(48, now, tmp_path)) == 1
