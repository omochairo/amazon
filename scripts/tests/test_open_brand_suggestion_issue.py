"""scripts/open_brand_suggestion_issue.py unit tests (A-6, epic #1356)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.open_brand_suggestion_issue import (
    MARKER,
    has_open_suggestion_issue,
    render_body,
    render_candidate_rows,
    render_yaml_diff_hint,
)


def test_render_candidate_rows_formats_token_imp_count_examples():
    cs = [{
        "token": "ロンビー",
        "impressions_sum": 150,
        "query_count": 2,
        "queries": [
            {"query": "ロンビー 知育", "impressions": 100, "clicks": 0, "ctr": 0.0, "position": 5.0},
            {"query": "ロンビー レビュー", "impressions": 50, "clicks": 0, "ctr": 0.0, "position": 7.0},
        ],
    }]
    rows = render_candidate_rows(cs)
    assert len(rows) == 1
    assert "ロンビー" in rows[0]
    assert "150" in rows[0]
    assert "2" in rows[0]
    assert "ロンビー 知育" in rows[0]


def test_render_yaml_diff_hint_emits_canonical_aliases_skeleton():
    cs = [{
        "token": "ロンビー",
        "impressions_sum": 150,
        "query_count": 2,
        "queries": [],
    }]
    s = render_yaml_diff_hint(cs)
    assert "canonical: ロンビー" in s
    assert "aliases: [ロンビー]" in s
    assert "tier: D" in s


def test_render_yaml_diff_hint_empty():
    assert "no candidates" in render_yaml_diff_hint([])


def test_render_body_includes_marker_and_epic_link():
    data = {
        "source_range": {"start": "2026-05-26", "end": "2026-06-01"},
        "params": {
            "min_impressions": 20, "min_token_len": 3, "top_n": 10,
            "taxonomy_term_count": 600,
        },
        "candidates": [{
            "token": "ロンビー",
            "impressions_sum": 150,
            "query_count": 2,
            "queries": [],
        }],
    }
    body = render_body(data)
    assert f"<!-- {MARKER} -->" in body
    assert "epic: #1356" in body
    assert "#1341" in body
    assert "ロンビー" in body


def test_render_body_with_no_candidates_uses_placeholder():
    data = {
        "source_range": {"start": "?", "end": "?"},
        "params": {"min_impressions": 20, "min_token_len": 3, "top_n": 10,
                   "taxonomy_term_count": 0},
        "candidates": [],
    }
    body = render_body(data)
    assert "(none)" in body


def test_has_open_suggestion_issue_true():
    fake = {"items": [{"body": f"<!-- {MARKER} -->\n..."}]}
    with patch("scripts.open_brand_suggestion_issue.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   args=[], returncode=0, stdout=json.dumps(fake), stderr="")):
        assert has_open_suggestion_issue("repo") is True


def test_has_open_suggestion_issue_false():
    with patch("scripts.open_brand_suggestion_issue.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   args=[], returncode=0, stdout=json.dumps({"items": []}), stderr="")):
        assert has_open_suggestion_issue("repo") is False
