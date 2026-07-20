"""scripts/comment_uniqueness_audit.py unit tests (#3203 Phase 3).

外部 gh CLI 呼び出しはせず、render / parse / dedup ロジックのみを検証する。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.comment_uniqueness_audit import (
    TRACKER_MARKER,
    WEEK_MARKER_PREFIX,
    find_tracker_issue_number,
    has_existing_week_comment,
    render_cohort_rows,
    render_comment_body,
    render_flagged_rows,
    resolve_tracker_issue_number,
)


def test_render_cohort_rows_includes_both_cohorts():
    stats = {
        "pre_v7": {"count": 10, "max_sim_p50": 0.8, "max_sim_p90": 0.95,
                   "centroid_sim_p50": 0.7, "centroid_sim_p90": 0.85},
        "post_v7": {"count": 3, "max_sim_p50": 0.6, "max_sim_p90": 0.8,
                    "centroid_sim_p50": 0.5, "centroid_sim_p90": 0.7},
    }
    out = render_cohort_rows(stats)
    lines = out.splitlines()
    assert "pre_v7" in lines[0] and "10" in lines[0] and "80.0%" in lines[0]
    assert "post_v7" in lines[1] and "3" in lines[1]


def test_render_cohort_rows_handles_missing_cohort_gracefully():
    out = render_cohort_rows({"pre_v7": {"count": 0}})
    assert "post_v7" in out
    assert "n/a" in out


def test_render_flagged_rows_empty_uses_placeholder():
    out = render_flagged_rows([])
    assert "閾値超えなし" in out


def test_render_flagged_rows_formats_entries():
    flagged = [
        {"asin": "B0AAAAAAAA", "page": "https://navi.omcha.jp/products/b0aaaaaaaa/",
         "cohort": "post_v7", "max_sim": 0.97, "centroid_sim": 0.91,
         "reasons": ["max_sim>0.95", "centroid_sim>0.9"]},
    ]
    out = render_flagged_rows(flagged)
    assert "B0AAAAAAAA" in out
    assert "post_v7" in out
    assert "97.0%" in out
    assert "max_sim>0.95" in out


def test_render_comment_body_includes_week_marker_and_disclaimer():
    payload = {
        "generated_at": "2026-07-19T20:00:00Z",
        "model": "cl-nagoya/ruri-v3-310m",
        "source_week": "2026-W29",
        "corpus_size": 1524,
        "thresholds": {"max_sim": 0.95, "centroid_sim": 0.90},
        "cohort_stats": {
            "pre_v7": {"count": 1500, "max_sim_p50": 0.7, "max_sim_p90": 0.9,
                       "centroid_sim_p50": 0.6, "centroid_sim_p90": 0.8},
            "post_v7": {"count": 24, "max_sim_p50": 0.6, "max_sim_p90": 0.8,
                        "centroid_sim_p50": 0.5, "centroid_sim_p90": 0.7},
        },
        "flagged": [],
    }
    body = render_comment_body(payload)
    marker = f"<!-- {WEEK_MARKER_PREFIX}2026-W29 -->"
    assert body.count(marker) == 2  # 先頭 dedup 用 + 末尾埋め込み
    assert "検証フェーズ" in body
    assert "自動リライトには未連動" in body
    assert "cl-nagoya/ruri-v3-310m" in body
    assert "1524" in body
    assert "owner の判断に委ねます" in body


def test_find_tracker_issue_number_extracts_marker_and_picks_min():
    fake_response = {
        "items": [
            {"number": 3300, "body": f"body with {TRACKER_MARKER} marker"},
            {"number": 3250, "body": f"another {TRACKER_MARKER} occurrence"},
        ]
    }

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fake_response), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        number = find_tracker_issue_number("omochairo/amazon")
    assert number == 3250


def test_find_tracker_issue_number_returns_none_when_absent():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"items": []}), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        number = find_tracker_issue_number("repo", sleeper=lambda s: None)
    assert number is None


def test_find_tracker_issue_number_retries_on_transient_zero_then_succeeds():
    # GitHub Search API のインデックス反映ラグ (answerability 監査で実際に観測済みの
    # 事象と同種): 初回0件→再試行で見つかるケース。
    fake_response = {"items": [{"number": 3301, "body": TRACKER_MARKER}]}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) < 2:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"items": []}), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fake_response), stderr="")

    sleeps = []
    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        number = find_tracker_issue_number("repo", sleeper=lambda s: sleeps.append(s))
    assert number == 3301
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_resolve_tracker_issue_number_uses_explicit_without_search():
    # 正の番号が与えられたら search/issues を一切叩かず直接使う (App token の Search
    # 可視性ラグを回避する既定経路)。
    def fail_run(cmd, **kwargs):  # pragma: no cover - 呼ばれたら失敗させる
        raise AssertionError("resolve should not call gh when issue_number is given")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fail_run):
        assert resolve_tracker_issue_number("repo", 3300) == 3300


def test_resolve_tracker_issue_number_falls_back_to_search_when_nonpositive():
    fake_response = {"items": [{"number": 3300, "body": TRACKER_MARKER}]}

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fake_response), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        assert resolve_tracker_issue_number("repo", 0, sleeper=lambda s: None) == 3300
        assert resolve_tracker_issue_number("repo", None, sleeper=lambda s: None) == 3300


def test_has_existing_week_comment_true_when_marker_present():
    comments = [{"body": f"unrelated\n<!-- {WEEK_MARKER_PREFIX}2026-W29 -->\nmore"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3250, "2026-W29") is True


def test_has_existing_week_comment_false_when_absent():
    comments = [{"body": "no marker here"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3250, "2026-W29") is False


def test_has_existing_week_comment_false_for_different_week_marker():
    # 別週のマーカーがある場合は同じ issue でも今週分は未投稿として扱う (冪等判定は週単位)
    comments = [{"body": f"<!-- {WEEK_MARKER_PREFIX}2026-W28 -->"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_uniqueness_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_week_comment("repo", 3250, "2026-W29") is False
