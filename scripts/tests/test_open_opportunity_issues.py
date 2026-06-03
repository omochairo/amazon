"""scripts/open_opportunity_issues.py unit tests (A-2, epic #1356)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.open_opportunity_issues import (
    MARKER_PREFIX,
    find_existing_taken_urls,
    render_body,
    render_query_rows,
)


def test_render_body_includes_marker_and_position_label():
    detected = {
        "page": "/products/B0XXX/",
        "impressions": 500,
        "clicks": 1,
        "ctr": 0.002,
        "position": 14.0,
        "top_queries": [],
    }
    body = render_body(detected, src_range={"start": "2026-05-26", "end": "2026-06-01"})
    assert f"<!-- {MARKER_PREFIX}/products/B0XXX/ -->" in body
    assert "14.0" in body
    assert "2 ページ目" in body
    assert "内部リンク" in body
    assert "epic: #1356" in body


def test_render_query_rows_handles_empty():
    assert "no per-query data" in render_query_rows([])


def test_render_query_rows_formats_each_row():
    rows = [
        {"query": "ベイブレード", "impressions": 80, "clicks": 0, "ctr": 0.0, "position": 13.0},
    ]
    out = render_query_rows(rows)
    assert "ベイブレード" in out
    assert "13.0" in out


def test_find_existing_taken_urls_extracts_marker():
    fake = {
        "items": [
            {"body": f"<!-- {MARKER_PREFIX}/products/B0AAA/ -->\nbody"},
            {"body": "no marker"},
        ]
    }
    with patch("scripts.open_opportunity_issues.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   args=[], returncode=0, stdout=json.dumps(fake), stderr="")):
        urls = find_existing_taken_urls("repo")
    assert urls == {"/products/B0AAA/"}
