"""check_engagement_queue_health.py の unit tests (#4791).

成り立つべき条件:
  1. 実データの定常状態 (news 在庫 0 の JIT / daily の鋸歯) で **鳴らない**。
     旧実装は直近 30 run 中 28 run で発火しており、閾値が分布の中央にあった。
  2. 手を打つべき状態でだけ鳴る (daily 在庫 0 / news の補充が途絶えた)。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scripts.check_engagement_queue_health import (
    DAILY_REFILL_STALE_HOURS,
    NEWS_REFILL_STALE_HOURS,
    assess,
    latest_created,
    load_rows,
)

NOW = dt.datetime(2026, 8, 9, 12, 0, tzinfo=dt.timezone.utc)


def _row(cat="daily", created="2026-08-09T06:00:00+09:00", published=None):
    r = {"id": f"eng-{cat}-{created}", "category": cat, "created_at": created,
         "text": "x"}
    if published:
        r["published_at"] = published
    return r


# ---------- 定常状態で鳴らない ----------

def test_jit_news_with_zero_stock_is_silent():
    """news は当日生成・当日消費。在庫 0 が正常なので鳴らしてはいけない。"""
    rows = [
        _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:56:00+00:00"),
        _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T02:30:00+00:00"),
        _row("daily", "2026-08-06T06:00:00+09:00"),
    ]
    st = assess(rows, NOW)
    assert st["by_cat"]["news"] == 0        # 在庫は 0
    assert st["alerts"] == []               # それでも無音


def test_daily_sawtooth_low_but_nonzero_is_silent():
    """daily は 7 件補充 → 3 件/日消費の鋸歯。残り 1 件でもまだ回っている。"""
    rows = [_row("daily", "2026-08-06T06:00:00+09:00")]
    rows += [_row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:00:00+00:00")]
    assert assess(rows, NOW)["alerts"] == []


def test_daily_stock_zero_alone_is_silent():
    """在庫 0 だけでは鳴らさない。

    replay 実測 (2026-07-28 06Z の threads) では在庫 0 は補充が届く直前の
    一瞬でも起き、その日は結局 3 件配信されていた = スロットは落ちていない。
    実害 (穴開け) は notify_engagement.py の ::warning:: が拾う。
    """
    rows = [_row("daily", "2026-08-08T06:00:00+09:00", "2026-08-09T01:00:00+00:00"),
            _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:00:00+00:00")]
    st = assess(rows, NOW)
    assert st["by_cat"]["daily"] == 0
    assert st["alerts"] == []


# ---------- 手を打つべき状態で鳴る ----------

def test_daily_refill_stalled_alerts():
    """daily の補充が 5 日以上途絶えたら補充レーンが死んでいる。"""
    rows = [_row("daily", "2026-08-03T06:00:00+09:00", "2026-08-04T01:00:00+00:00"),
            _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:00:00+00:00")]
    alerts = assess(rows, NOW)["alerts"]
    assert len(alerts) == 1
    assert "daily refill stalled" in alerts[0]


def test_daily_refill_within_observed_gap_is_silent():
    """実測の補充間隔は 2〜4 日。4 日程度では鳴らない。"""
    rows = [_row("daily", "2026-08-06T06:00:00+09:00"),
            _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:00:00+00:00")]
    assert assess(rows, NOW)["alerts"] == []


def test_news_refill_stalled_alerts():
    """在庫ではなく補充が途絶えたことで鳴る。"""
    rows = [_row("daily", "2026-08-09T06:00:00+09:00"),
            _row("news", "2026-08-07T06:00:00+09:00", "2026-08-07T07:00:00+00:00")]
    alerts = assess(rows, NOW)["alerts"]
    assert len(alerts) == 1
    assert "news refill stalled" in alerts[0]


def test_news_refill_within_one_cycle_is_silent():
    """実測の補充は毎日 06:00 JST。定常時の age は 24h 未満なので鳴らない。

    NOW=08-09 12:00Z に対し 08-09 06:00+09:00 (= 08-08 21:00Z) は 15h。
    """
    rows = [_row("daily", "2026-08-09T06:00:00+09:00"),
            _row("news", "2026-08-09T06:00:00+09:00", "2026-08-09T04:00:00+00:00")]
    assert assess(rows, NOW)["alerts"] == []


def test_news_refill_skipped_day_alerts():
    """補充が 1 日飛ぶと news スロット 2 枠が落ちるので鳴らす (39h > 36h)。"""
    rows = [_row("daily", "2026-08-09T06:00:00+09:00"),
            _row("news", "2026-08-08T06:00:00+09:00", "2026-08-08T07:00:00+00:00")]
    alerts = assess(rows, NOW)["alerts"]
    assert len(alerts) == 1 and "news refill stalled" in alerts[0]


def test_no_news_at_all_alerts():
    rows = [_row("daily", "2026-08-09T06:00:00+09:00")]
    alerts = assess(rows, NOW)["alerts"]
    assert any("no news rows have ever been generated" in a for a in alerts)


def test_both_lanes_alert_independently():
    rows = [_row("daily", "2026-08-01T06:00:00+09:00", "2026-08-01T07:00:00+00:00"),
            _row("news", "2026-08-01T06:00:00+09:00", "2026-08-01T07:00:00+00:00")]
    assert len(assess(rows, NOW)["alerts"]) == 2


# ---------- 補助 ----------

def test_latest_created_ignores_other_categories():
    rows = [_row("news", "2026-08-01T06:00:00+09:00"),
            _row("daily", "2026-08-09T06:00:00+09:00")]
    assert latest_created(rows, "news").date() == dt.date(2026, 8, 1)


def test_latest_created_none_when_absent():
    assert latest_created([_row("daily")], "news") is None


def test_latest_created_skips_broken_timestamps():
    rows = [_row("news", "garbage"), _row("news", "2026-08-09T06:00:00+09:00")]
    assert latest_created(rows, "news") is not None


def test_naive_timestamp_treated_as_utc():
    rows = [_row("daily", "2026-08-09T06:00:00"),
            _row("news", "2026-08-09T06:00:00")]
    assert assess(rows, NOW)["alerts"] == []


def test_load_rows_skips_broken_lines(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text('{"id":1}\nnot json\n[]\n\n{"id":2}\n', encoding="utf-8")
    assert [r["id"] for r in load_rows(p)] == [1, 2]


def test_load_rows_missing_file(tmp_path):
    assert load_rows(tmp_path / "nope.jsonl") == []


def test_unknown_category_counted_as_other():
    rows = [_row("weird", "2026-08-09T06:00:00+09:00"),
            _row("daily", "2026-08-09T06:00:00+09:00"),
            _row("news", "2026-08-09T06:00:00+09:00")]
    assert assess(rows, NOW)["by_cat"]["other"] == 1


# ---------- 較正: 実 queue で鳴らない ----------

@pytest.mark.parametrize("name", ["engagement_queue_x.jsonl", "engagement_queue_threads.jsonl"])
def test_real_queue_is_quiet(name):
    """2026-08-09 時点の実 queue で 1 件も鳴らないこと (#4791 の較正根拠)。

    旧実装はこの同じデータで `news queue low (0<2)` を鳴らしていた。
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    p = repo_root / "data" / name
    if not p.exists():  # pragma: no cover
        pytest.skip(f"{name} not present")
    rows = load_rows(p)
    st = assess(rows, dt.datetime(2026, 8, 9, 12, 0, tzinfo=dt.timezone.utc))
    assert st["alerts"] == [], (name, st["by_cat"], st["news_age_hours"], st["alerts"])


def test_thresholds_sit_above_the_observed_cadence():
    """閾値を実測 cadence の上に置く (分布の中央に置かない)。"""
    # news は毎日 06:00 JST 補充 = 定常 age < 24h。1 日飛べば鳴る幅。
    assert 24 < NEWS_REFILL_STALE_HOURS < 48
    # daily は 2〜4 日おきの batch 補充 = 定常 age < 96h。その上に置く。
    assert DAILY_REFILL_STALE_HOURS > 96
