"""scripts/comment_information_gain_audit.py unit tests (#4841 S3)。

外部 gh CLI 呼び出しはせず、render / dedup ロジックのみを検証する。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.comment_information_gain_audit import (
    DEFAULT_TRACKER_ISSUE,
    WEEK_MARKER_PREFIX,
    has_existing_week_comment,
    render_comment_body,
    render_material_row,
)


def _payload():
    return {
        "source_week": "2026-W38",
        "model": "gemma4:26b-a4b-it-qat",
        "summary": {
            "target_count": 40,
            "processed_count": 38,
            "failed_count": 2,
            "failure_ratio": 0.05,
            "median_unique_and_supported_count": 6,
            "median_unique_and_supported_rate": 0.32,
            "by_experience_material": {
                "with_material": {"count": 20, "median_unique_and_supported_count": 8,
                                   "median_unique_and_supported_rate": 0.4},
                "without_material": {"count": 18, "median_unique_and_supported_count": 4,
                                      "median_unique_and_supported_rate": 0.2},
                "unknown": {"count": 0, "median_unique_and_supported_count": None,
                            "median_unique_and_supported_rate": None},
            },
            "unsupported_classification_totals": {
                "rhetorical_or_time_dependent": 30, "factual_claim": 4, "unresolved": 1,
            },
        },
        "failed": [{"asin": "B0AAAAAAAA", "reason": "TruncationError: ..."}],
    }


def test_default_tracker_issue_is_3300():
    """#4841 実装依頼 S3: 新規 tracker issue は作らず既存 #3300 に相乗りする。"""
    assert DEFAULT_TRACKER_ISSUE == 3300


def test_render_material_row_formats_count_and_stats():
    row = render_material_row("あり", {"count": 20, "median_unique_and_supported_count": 8,
                                        "median_unique_and_supported_rate": 0.4})
    assert "あり" in row
    assert "20" in row
    assert "8" in row
    assert "40.0%" in row


def test_render_material_row_handles_missing_group():
    row = render_material_row("なし", None)
    assert "0" in row
    assert "n/a" in row


def test_render_comment_body_includes_week_marker_and_disclaimer():
    body = render_comment_body(_payload())
    assert f"<!-- {WEEK_MARKER_PREFIX}2026-W38 -->" in body
    assert "観測専用" in body
    assert "自動リライトには未連動" in body


def test_render_comment_body_uses_different_marker_from_uniqueness_audit():
    """凡庸度監査 (uniqueness-audit:) と別マーカーであること (同じ issue に共存するため)。"""
    assert WEEK_MARKER_PREFIX == "information-gain-audit:"
    body = render_comment_body(_payload())
    assert "uniqueness-audit:" not in body


def test_render_comment_body_lists_failed_articles():
    body = render_comment_body(_payload())
    assert "B0AAAAAAAA" in body
    assert "TruncationError" in body


def test_render_comment_body_omits_failed_section_when_none():
    payload = _payload()
    payload["failed"] = []
    body = render_comment_body(payload)
    assert "失敗した記事" not in body


def test_has_existing_week_comment_true_when_marker_present():
    comments = [{"body": f"unrelated\n<!-- {WEEK_MARKER_PREFIX}2026-W38 -->\nmore"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3300, "2026-W38") is True


def test_has_existing_week_comment_false_for_different_week():
    comments = [{"body": f"<!-- {WEEK_MARKER_PREFIX}2026-W37 -->"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3300, "2026-W38") is False


def test_has_existing_week_comment_ignores_uniqueness_audit_marker():
    """同じ issue に同居する凡庸度監査の週マーカーと混同しないこと。"""
    comments = [{"body": "<!-- uniqueness-audit:2026-W38 -->"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3300, "2026-W38") is False
