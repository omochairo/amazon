"""scripts/report_census_404.py unit tests (棚卸しレポート, Refs #3331, #3988)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.append_census_history import CENSUS_HISTORY_FILE
from scripts.report_census_404 import (
    build_inbound_report,
    count_inbound_links,
    extract_404_urls,
    load_history_rows,
    run,
    summarize_404_trend,
    summarize_last_crawl_distribution,
)


@pytest.fixture()
def census_fixture():
    return {
        "fetched_at": "2026-07-26T23:08:05.707508+00:00",
        "totals": {"sitemap_urls": 1705, "inspected": 1705, "errors": 0, "indexed": 1213, "not_indexed": 492},
        "not_indexed_urls": [
            {
                "url": "https://navi.omcha.jp/products/b00005bhog/",
                "coverage_state": "URL が Google に認識されていません",
                "last_crawl_time": "(none)",
                "google_canonical": "(none)",
            },
            {
                "url": "https://navi.omcha.jp/products/b0899jmq7f/",
                "coverage_state": "見つかりませんでした（404）",
                "last_crawl_time": "2026-06-05T09:16:21Z",
                "google_canonical": "(none)",
            },
            {
                "url": "https://navi.omcha.jp/products/b0by2hlw37/",
                "coverage_state": "見つかりませんでした（404）",
                "last_crawl_time": "2026-07-03T02:24:02Z",
                "google_canonical": "https://navi.omcha.jp/products/b0by2hlw37/",
            },
        ],
    }


# ---------------------------------------------------------------------------
# 1. 404 認識件数の推移
# ---------------------------------------------------------------------------

def test_load_history_rows_missing_file_returns_empty(tmp_path):
    assert load_history_rows(tmp_path / CENSUS_HISTORY_FILE) == []


def test_summarize_trend_single_row_does_not_crash():
    rows = [{"date": "2026-07-26", "not_found_404": 73, "circuit_breaker_tripped": False}]
    trend = summarize_404_trend(rows)
    assert trend == [{"date": "2026-07-26", "not_found_404": 73, "circuit_breaker_tripped": False}]


def test_summarize_trend_sorted_and_flags_partial_run(tmp_path):
    path = tmp_path / CENSUS_HISTORY_FILE
    path.write_text(
        '{"date": "2026-07-26", "not_found_404": 73, "circuit_breaker_tripped": false}\n'
        '{"date": "2026-07-19", "not_found_404": 74, "circuit_breaker_tripped": true}\n',
        encoding="utf-8",
    )
    rows = load_history_rows(path)
    trend = summarize_404_trend(rows)
    assert [r["date"] for r in trend] == ["2026-07-19", "2026-07-26"]
    assert trend[0]["circuit_breaker_tripped"] is True
    assert trend[1]["circuit_breaker_tripped"] is False


def test_load_history_rows_tolerates_corrupt_line(tmp_path):
    path = tmp_path / CENSUS_HISTORY_FILE
    path.write_text('{"date": "2026-07-26", "not_found_404": 73}\nnot json\n', encoding="utf-8")
    rows = load_history_rows(path)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-26"


# ---------------------------------------------------------------------------
# 2. 現行 census の 404 URL 抽出 + last_crawl_time 分布
# ---------------------------------------------------------------------------

def test_extract_404_urls_filters_only_404_state(census_fixture):
    urls, unknown = extract_404_urls(census_fixture)
    assert len(urls) == 2
    assert {u["url"] for u in urls} == {
        "https://navi.omcha.jp/products/b0899jmq7f/",
        "https://navi.omcha.jp/products/b0by2hlw37/",
    }
    assert unknown == {}


def test_extract_404_urls_zero_when_no_404(census_fixture):
    census_fixture["not_indexed_urls"] = [census_fixture["not_indexed_urls"][0]]
    urls, unknown = extract_404_urls(census_fixture)
    assert urls == []
    assert unknown == {}


def test_extract_404_urls_flags_unknown_coverage_state(census_fixture):
    census_fixture["not_indexed_urls"].append({
        "url": "https://navi.omcha.jp/products/b0zzzzzzzz/",
        "coverage_state": "謎の新しい状態",
        "last_crawl_time": "(none)",
        "google_canonical": "(none)",
    })
    urls, unknown = extract_404_urls(census_fixture)
    assert len(urls) == 2  # 未知の状態は 404 集合に含めない
    assert unknown == {"謎の新しい状態": 1}


def test_summarize_last_crawl_distribution_buckets_by_year_month(census_fixture):
    urls, _ = extract_404_urls(census_fixture)
    dist = summarize_last_crawl_distribution(urls)
    assert dist == {"2026-06": 1, "2026-07": 1}


def test_summarize_last_crawl_distribution_empty_when_no_urls():
    assert summarize_last_crawl_distribution([]) == {}


def test_summarize_last_crawl_distribution_handles_none_and_unparsable():
    urls = [
        {"url": "a", "last_crawl_time": "(none)"},
        {"url": "b", "last_crawl_time": None},
        {"url": "c", "last_crawl_time": "garbage"},
    ]
    dist = summarize_last_crawl_distribution(urls)
    assert dist == {"never_crawled": 2, "unparsable": 1}


# ---------------------------------------------------------------------------
# 3. 内部被リンク数
# ---------------------------------------------------------------------------

def _write_article(articles_dir: pathlib.Path, stem: str, data: dict | None = None) -> None:
    articles_dir.mkdir(parents=True, exist_ok=True)
    (articles_dir / f"{stem}.json").write_text(json.dumps(data or {"narrative": {}}), encoding="utf-8")


def test_count_inbound_links_missing_dir_returns_none_error(tmp_path):
    counts, error = count_inbound_links(tmp_path / "nope")
    assert counts is None
    assert error is not None


def test_count_inbound_links_tallies_target_asin(tmp_path):
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")
    _write_article(articles_dir, "2026-07-01-B0BY2HLW37")
    (articles_dir / "2026-07-01-B0899JMQ7F.seo.json").write_text(
        json.dumps({"internal_link_suggestions": [
            {"target_asin": "B0BY2HLW37", "anchor_text": "x"},
        ]}), encoding="utf-8",
    )
    counts, error = count_inbound_links(articles_dir)
    assert error is None
    assert counts["B0BY2HLW37"] == 1
    assert counts["B0899JMQ7F"] == 0  # 記事として存在するが被リンクは 0


def test_build_inbound_report_zero_when_no_suggestions(census_fixture, tmp_path):
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")
    _write_article(articles_dir, "2026-07-01-B0BY2HLW37")
    counts, error = count_inbound_links(articles_dir)
    assert error is None
    urls, _ = extract_404_urls(census_fixture)
    report = build_inbound_report(urls, counts)
    assert all(r["inbound_links"] == 0 for r in report)


def test_build_inbound_report_unknown_when_counts_unavailable(census_fixture):
    urls, _ = extract_404_urls(census_fixture)
    report = build_inbound_report(urls, None)
    assert all(r["inbound_links"] == "unknown" for r in report)


def test_build_inbound_report_sorted_ascending_by_count():
    urls = [
        {"url": "https://navi.omcha.jp/products/b0899jmq7f/"},
        {"url": "https://navi.omcha.jp/products/b0by2hlw37/"},
    ]
    counts = {"B0899JMQ7F": 3, "B0BY2HLW37": 0}
    report = build_inbound_report(urls, counts)
    assert [r["asin"] for r in report] == ["B0BY2HLW37", "B0899JMQ7F"]


# ---------------------------------------------------------------------------
# run() 統合: 0 件・履歴 1 行でも正常終了すること
# ---------------------------------------------------------------------------

def test_run_zero_404_urls_completes_normally(tmp_path):
    census_path = tmp_path / "gsc_index_census.json"
    census_path.write_text(json.dumps({
        "fetched_at": "2026-07-26T23:08:05+00:00",
        "totals": {"sitemap_urls": 10, "inspected": 10, "indexed": 10, "not_indexed": 0, "errors": 0},
        "not_indexed_urls": [],
    }), encoding="utf-8")
    history_dir = tmp_path / "history"
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")

    result = run(census_path, history_dir, articles_dir)
    assert result["count_404"] == 0
    assert result["urls_404"] == []
    assert result["inbound_links"] == []
    assert result["inbound_links_error"] is None
    assert result["trend"] == []  # history 未作成


def test_run_missing_census_file_completes_normally(tmp_path):
    result = run(tmp_path / "nope.json", tmp_path / "history", tmp_path / "articles")
    assert result["count_404"] == 0
    assert result["inbound_links_error"] is not None  # articles dir も無いので算出不能


def test_run_history_single_line_does_not_crash(tmp_path):
    census_path = tmp_path / "gsc_index_census.json"
    census_path.write_text(json.dumps({
        "fetched_at": "2026-07-26T23:08:05+00:00",
        "totals": {"sitemap_urls": 10, "inspected": 10, "indexed": 9, "not_indexed": 1, "errors": 0},
        "not_indexed_urls": [],
    }), encoding="utf-8")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / CENSUS_HISTORY_FILE).write_text(
        '{"date": "2026-07-26", "not_found_404": 73, "circuit_breaker_tripped": false}\n',
        encoding="utf-8",
    )
    result = run(census_path, history_dir, tmp_path / "articles")
    assert result["trend"] == [
        {"date": "2026-07-26", "not_found_404": 73, "circuit_breaker_tripped": False},
    ]
