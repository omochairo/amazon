"""scripts/open_article_feedback_issue.py unit tests (issue #2051)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.open_article_feedback_issue import (
    MARKER_PREFIX,
    find_open_issue_number,
    render_body,
    render_comment,
    render_rows,
    resolve_month,
)


def test_resolve_month_from_source_range_end():
    assert resolve_month({"start": "2026-06-01", "end": "2026-06-30"}) == "2026-06"


def test_resolve_month_falls_back_to_today_when_missing():
    month = resolve_month(None)
    assert len(month) == 7 and month[4] == "-"


def test_render_rows_with_rating():
    detected = [{"page_path": "/a/", "total": 10, "good": 2, "ok": 3, "bad": 5, "bad_ratio": 0.5}]
    rows = render_rows(detected, rating_available=True)
    assert "/a/" in rows[0]
    assert "50.0%" in rows[0]


def test_render_rows_without_rating():
    detected = [{"page_path": "/a/", "total": 10}]
    rows = render_rows(detected, rating_available=False)
    assert rows == ["| 1 | `/a/` | 10 |"]


def test_render_body_includes_marker_and_epic_link():
    report = {
        "source_range": {"start": "2026-06-01", "end": "2026-06-30"},
        "rating_dimension_available": True,
        "totals": {"events": 10, "pages": 1},
        "detected": [
            {"page_path": "/a/", "total": 10, "good": 2, "ok": 3, "bad": 5, "bad_ratio": 0.5},
        ],
    }
    body = render_body(report, month="2026-06")
    assert f"<!-- {MARKER_PREFIX}2026-06 -->" in body
    assert "#2051" in body
    assert "/a/" in body
    assert "GA4 admin 設定" not in body  # rating 取得済みなので admin 手順は出さない


def test_render_body_without_rating_includes_admin_steps():
    report = {
        "source_range": {"start": "2026-06-01", "end": "2026-06-30"},
        "rating_dimension_available": False,
        "totals": {"events": 10, "pages": 1},
        "detected": [{"page_path": "/a/", "total": 10}],
    }
    body = render_body(report, month="2026-06")
    assert "GA4 admin 設定" in body
    assert "カスタム ディメンションを作成" in body


def test_render_body_no_detected_uses_placeholder():
    report = {
        "source_range": {"start": "2026-06-01", "end": "2026-06-30"},
        "rating_dimension_available": True,
        "totals": {"events": 0, "pages": 0},
        "detected": [],
    }
    body = render_body(report, month="2026-06")
    assert "該当ページなし" in body


def test_render_comment_is_shorter_update_style():
    report = {
        "rating_dimension_available": True,
        "totals": {"events": 20, "pages": 2},
        "detected": [
            {"page_path": "/a/", "total": 10, "good": 2, "ok": 3, "bad": 5, "bad_ratio": 0.5},
        ],
    }
    comment = render_comment(report, month="2026-06")
    assert f"<!-- {MARKER_PREFIX}2026-06:update -->" in comment
    assert "/a/" in comment


def test_find_open_issue_number_returns_number_when_found():
    fake = {"items": [{"number": 4242}]}
    with patch("scripts.open_article_feedback_issue.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   args=[], returncode=0, stdout=json.dumps(fake), stderr="")):
        assert find_open_issue_number("owner/repo", "2026-06") == 4242


def test_find_open_issue_number_returns_none_when_empty():
    with patch("scripts.open_article_feedback_issue.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   args=[], returncode=0, stdout=json.dumps({"items": []}), stderr="")):
        assert find_open_issue_number("owner/repo", "2026-06") is None
