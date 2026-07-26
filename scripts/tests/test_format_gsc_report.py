"""scripts/format_gsc_report.py unit tests (#3988 B-1: site-wide totals rendering)."""
from __future__ import annotations

import pytest

from scripts.format_gsc_report import render


@pytest.fixture()
def gsc_fixture():
    return {
        "fetched_at": "2026-07-26T00:00:00+00:00",
        "site_url": "sc-domain:omcha.jp",
        "range": {"start": "2026-07-19", "end": "2026-07-20", "days": 1, "delay_days": 3},
        "totals": {
            "queries": 2,
            "pages": 1,
            "clicks_sum": 5,
            "impressions_sum": 150,
            "clicks_sitewide": 500,
            "impressions_sitewide": 15000,
            "ctr_sitewide": 0.0333,
            "position_sitewide": 8.4,
            "truncated_pages": True,
            "truncated_queries": False,
        },
        "by_query": [
            {"query": "知育玩具", "clicks": 3, "impressions": 80, "ctr": 0.0375, "position": 4.0},
        ],
        "by_page": [
            {"page": "/products/B0X/", "clicks": 5, "impressions": 150, "ctr": 0.033, "position": 5.0},
        ],
        "by_device": [],
        "opportunity_pages": [],
    }


def test_render_shows_sitewide_values(gsc_fixture):
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    assert "サイト全体 clicks" in out
    assert "500" in out
    assert "サイト全体 impressions" in out
    assert "15,000" in out
    assert "サイト全体 CTR" in out
    assert "3.3%" in out
    assert "サイト全体 平均順位" in out
    assert "8.4" in out


def test_render_sitewide_none_renders_na(gsc_fixture):
    gsc_fixture["totals"].update({
        "clicks_sitewide": None,
        "impressions_sitewide": None,
        "ctr_sitewide": None,
        "position_sitewide": None,
    })
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    lines = out.splitlines()
    sitewide_lines = [
        ln for ln in lines
        if ln.startswith("- サイト全体")
    ]
    assert len(sitewide_lines) == 4
    for ln in sitewide_lines:
        assert "n/a" in ln


def test_render_truncation_note_shown_when_truncated_pages_true(gsc_fixture):
    gsc_fixture["totals"]["truncated_pages"] = True
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    assert "打ち切られています" in out


def test_render_truncation_note_absent_when_truncated_pages_false(gsc_fixture):
    gsc_fixture["totals"]["truncated_pages"] = False
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    assert "打ち切られています" not in out


def test_render_keeps_top_n_sum_labels_relabeled(gsc_fixture):
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    assert "上位ページ合計 clicks" in out
    assert "上位ページ合計 impressions" in out


def test_render_label_still_appends_suffix_to_heading(gsc_fixture):
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20, label="WP omcha.jp")
    first_line = out.splitlines()[0]
    assert first_line.startswith("## GSC 週次レポート — WP omcha.jp")


def test_render_no_label_heading_has_no_suffix(gsc_fixture):
    out = render(gsc_fixture, top_query=30, top_page=20, top_opp=20)
    first_line = out.splitlines()[0]
    assert first_line.startswith("## GSC 週次レポート (")
    assert " — " not in first_line
