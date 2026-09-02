"""A レーン Issue の有効期限 (epic #1356) の unit test。

scripts/_analytics_issue_expiry.py (marker の生成・読み取り) と
scripts/close_expired_analytics_issues.py (期限切れの選別) を対象にする。
gh CLI は呼ばない。
"""
from __future__ import annotations

import datetime as dt

from scripts._analytics_issue_expiry import (
    DEFAULT_TTL_DAYS,
    MARKER_PREFIX,
    expiry_date,
    expiry_marker,
    expiry_note,
    find_expiry,
    is_expired,
)
from scripts.close_expired_analytics_issues import select_expired


def test_expiry_date_is_range_end_plus_ttl():
    assert expiry_date({"start": "2026-08-23", "end": "2026-08-30"}) == dt.date(2026, 9, 13)
    assert DEFAULT_TTL_DAYS == 14


def test_expiry_date_honours_custom_ttl():
    assert expiry_date({"end": "2026-08-30"}, ttl_days=7) == dt.date(2026, 9, 6)


def test_expiry_date_none_when_range_unusable():
    # 期限を決められないときに勝手な日付を作らない (誤 close を防ぐ)
    assert expiry_date(None) is None
    assert expiry_date({}) is None
    assert expiry_date({"end": ""}) is None
    assert expiry_date({"end": "2026-13-99"}) is None
    assert expiry_date({"end": 20260830}) is None


def test_expiry_marker_roundtrips_through_find_expiry():
    marker = expiry_marker({"end": "2026-08-30"})
    assert marker == f"<!-- {MARKER_PREFIX}2026-09-13 -->"
    assert find_expiry(f"本文\n{marker}\n続き") == dt.date(2026, 9, 13)


def test_expiry_marker_empty_when_no_range():
    assert expiry_marker({}) == ""
    assert expiry_note({}) == []


def test_expiry_note_mentions_the_date():
    note = expiry_note({"end": "2026-08-30"})
    assert note and any("2026-09-13" in line for line in note)


def test_find_expiry_ignores_unmarked_body():
    assert find_expiry(None) is None
    assert find_expiry("マーカーの無い本文") is None
    # 別の検出器の dedup マーカーを期限と誤読しない
    assert find_expiry("<!-- a5-orphan:https://navi.omcha.jp/x/ -->") is None


def test_is_expired_is_strictly_after_the_date():
    body = f"<!-- {MARKER_PREFIX}2026-09-13 -->"
    assert not is_expired(body, today=dt.date(2026, 9, 12))
    assert not is_expired(body, today=dt.date(2026, 9, 13))  # 当日は閉じない
    assert is_expired(body, today=dt.date(2026, 9, 14))


def _item(number: int, body: str, title: str = "t") -> dict:
    return {"number": number, "body": body, "title": title}


def test_select_expired_skips_unmarked_and_future():
    items = [
        _item(1, f"<!-- {MARKER_PREFIX}2026-09-01 -->"),   # 期限切れ
        _item(2, f"<!-- {MARKER_PREFIX}2026-12-01 -->"),   # まだ有効
        _item(3, "人が立てた Issue (マーカー無し)"),        # 触らない
    ]
    got = select_expired(items, today=dt.date(2026, 9, 10))
    assert [d["number"] for d in got] == [1]


def test_select_expired_sorted_oldest_first():
    items = [
        _item(1, f"<!-- {MARKER_PREFIX}2026-09-05 -->"),
        _item(2, f"<!-- {MARKER_PREFIX}2026-08-20 -->"),
        _item(3, f"<!-- {MARKER_PREFIX}2026-09-01 -->"),
    ]
    got = select_expired(items, today=dt.date(2026, 9, 10))
    # --max-close で切り捨てるとき、古いものから消化されること
    assert [d["number"] for d in got] == [2, 3, 1]


def test_select_expired_empty_when_nothing_due():
    items = [_item(1, f"<!-- {MARKER_PREFIX}2026-09-30 -->")]
    assert select_expired(items, today=dt.date(2026, 9, 10)) == []


def test_weekly_ttl_is_longer_than_per_url():
    from scripts._analytics_issue_expiry import WEEKLY_TTL_DAYS

    assert WEEKLY_TTL_DAYS > DEFAULT_TTL_DAYS
    assert expiry_date({"end": "2026-08-30"}, ttl_days=WEEKLY_TTL_DAYS) == dt.date(2026, 9, 27)


def _run_cli(*args: str) -> str:
    import subprocess
    import sys

    res = subprocess.run(
        [sys.executable, "-m", "scripts._analytics_issue_expiry", *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return res.stdout.strip()


def test_cli_emits_marker_for_weekly_and_default():
    # 週次レポート step が shell から呼ぶ経路。TTL を bash 側に再実装しないための口
    assert _run_cli("--end", "2026-08-30", "--weekly") == f"<!-- {MARKER_PREFIX}2026-09-27 -->"
    assert _run_cli("--end", "2026-08-30") == f"<!-- {MARKER_PREFIX}2026-09-13 -->"


def test_cli_emits_empty_and_succeeds_when_date_unusable():
    # workflow は空文字を「期限なし」として扱うので、ここで落ちてはいけない
    assert _run_cli("--end", "") == ""
    assert _run_cli("--end", "2026-13-99") == ""
