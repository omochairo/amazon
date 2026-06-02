"""scripts/open_low_ctr_issues.py unit tests (A-1, epic #1356).

外部 gh CLI 呼び出しはせず、render / parse / dedup ロジックのみを検証する。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.open_low_ctr_issues import (
    MARKER_PREFIX,
    find_existing_taken_urls,
    render_body,
    render_query_rows,
)


def test_render_query_rows_empty_uses_placeholder():
    out = render_query_rows([])
    assert "no per-query data" in out


def test_render_query_rows_formats_each_row():
    rows = [
        {"query": "おもちゃ", "impressions": 100, "clicks": 5, "ctr": 0.05, "position": 4.0},
        {"query": "知育", "impressions": 50, "clicks": 1, "ctr": 0.02, "position": 7.5},
    ]
    out = render_query_rows(rows)
    lines = out.splitlines()
    assert len(lines) == 2
    assert "おもちゃ" in lines[0]
    assert "5.00%" in lines[0]
    assert "4.0" in lines[0]
    assert "知育" in lines[1]


def test_render_body_includes_marker():
    detected = {
        "page": "/products/B0XXX/",
        "impressions": 500,
        "clicks": 1,
        "ctr": 0.002,
        "position": 4.0,
        "ratio_to_baseline": 0.2,
        "top_queries": [],
    }
    body = render_body(detected, baseline=0.01, threshold=0.005,
                       src_range={"start": "2026-05-26", "end": "2026-06-01"})
    assert f"<!-- {MARKER_PREFIX}/products/B0XXX/ -->" in body
    assert "/products/B0XXX/" in body
    assert "2026-05-26" in body
    assert "epic: #1356" in body


def test_render_body_handles_missing_ratio():
    detected = {
        "page": "/p/",
        "impressions": 100,
        "clicks": 0,
        "ctr": 0.0,
        "position": 5.0,
        "ratio_to_baseline": None,
        "top_queries": [],
    }
    body = render_body(detected, baseline=0.0, threshold=0.005, src_range={})
    assert "n/a" in body


def test_find_existing_taken_urls_extracts_marker():
    fake_response = {
        "items": [
            {"body": f"<!-- {MARKER_PREFIX}/products/B0AAA/ -->\nbody text"},
            {"body": f"<!-- {MARKER_PREFIX}/products/B0BBB/ -->"},
            {"body": "unrelated issue, no marker"},
        ]
    }

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(fake_response), stderr=""
        )

    with patch("scripts.open_low_ctr_issues.subprocess.run", side_effect=fake_run):
        urls = find_existing_taken_urls("omochairo/amazon")
    assert urls == {"/products/B0AAA/", "/products/B0BBB/"}


def test_find_existing_taken_urls_handles_multiple_markers_in_one_body():
    fake_response = {
        "items": [
            {"body": (
                f"<!-- {MARKER_PREFIX}/a/ -->\n"
                "some text\n"
                f"<!-- {MARKER_PREFIX}/b/ -->\n"
            )},
        ]
    }

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(fake_response), stderr=""
        )

    with patch("scripts.open_low_ctr_issues.subprocess.run", side_effect=fake_run):
        urls = find_existing_taken_urls("repo")
    assert urls == {"/a/", "/b/"}


def test_find_existing_taken_urls_empty():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"items": []}), stderr=""
        )

    with patch("scripts.open_low_ctr_issues.subprocess.run", side_effect=fake_run):
        urls = find_existing_taken_urls("repo")
    assert urls == set()
