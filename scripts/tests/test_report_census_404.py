"""scripts/report_census_404.py unit tests (棚卸しレポート, Refs #3331, #3988)."""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts import report_census_404
from scripts.append_census_history import CENSUS_HISTORY_FILE
from scripts.report_census_404 import (
    INBOUND_SOURCE_PRIMARY_GRAPH,
    INBOUND_SOURCE_SIDECAR,
    INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD,
    build_inbound_report,
    build_inbound_report_from_graph,
    count_inbound_suggestions,
    extract_404_urls,
    load_history_rows,
    load_inbound_links_graph,
    render_report,
    run,
    summarize_404_trend,
    summarize_last_crawl_distribution,
)


@pytest.fixture(autouse=True)
def _isolate_inbound_links_default(tmp_path, monkeypatch):
    """run() の inbound_links_path 既定値を、リポジトリ実体から切り離す。

    run() は第4引数省略時に DEFAULT_INBOUND_LINKS_JSON
    ("data/site_audit/inbound_links.json") をカレントディレクトリ基準で読む。
    このため引数を渡さない run() テストは「CI の cwd = リポジトリ root に
    その実ファイルがあるか」に結果が左右される。

    2026-08-04 の cb138f1860 (#4516 weekly site health audit) で当該ファイルが
    初めてリポジトリにコミットされ、以降 CI では一次ソース (graph) 分岐が
    常に選ばれるようになった。結果 inbound_suggestions_error が常に None と
    なり test_run_missing_census_file_completes_normally が全ブランチで fail
    (#4069)。実装の退行ではなく、テストが実データを読んでいたのが原因。

    存在しないパスへ倒すことで、引数を省略した run() は必ず sidecar
    フォールバック分岐を通る (= 各テストが宣言した前提どおりになる)。
    一次ソース分岐を検証するテストは明示的に第4引数を渡しており影響しない。
    """
    monkeypatch.setattr(
        report_census_404,
        "DEFAULT_INBOUND_LINKS_JSON",
        str(tmp_path / "absent" / "inbound_links.json"),
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
# 3. sidecar 内部リンク候補の被参照数 + 供給源カバレッジ
#
# #4381 レビュー指摘: internal_links.py (omcha.jp WP REST API クライアント。
# navi 側の内部リンクグラフとは無関係) は使わない。sidecar
# (internal_link_suggestions) が正しい供給源だが、実測でコーパスの一部しか
# カバーしていないため、カバレッジが低い状態での「0」は「未測定」として
# unknown 扱いする (0 に潰さない)。
# ---------------------------------------------------------------------------

def _write_article(articles_dir: pathlib.Path, stem: str, data: dict | None = None) -> None:
    articles_dir.mkdir(parents=True, exist_ok=True)
    (articles_dir / f"{stem}.json").write_text(json.dumps(data or {"narrative": {}}), encoding="utf-8")


def test_count_inbound_suggestions_missing_dir_returns_none_error(tmp_path):
    counts, coverage, error = count_inbound_suggestions(tmp_path / "nope")
    assert counts is None
    assert coverage == {}
    assert error is not None


def test_count_inbound_suggestions_tallies_target_asin_and_coverage_stats(tmp_path):
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")
    _write_article(articles_dir, "2026-07-01-B0BY2HLW37")
    (articles_dir / "2026-07-01-B0899JMQ7F.seo.json").write_text(
        json.dumps({"internal_link_suggestions": [
            {"target_asin": "B0BY2HLW37", "anchor_text": "x"},
        ]}), encoding="utf-8",
    )
    counts, coverage, error = count_inbound_suggestions(articles_dir)
    assert error is None
    assert counts["B0BY2HLW37"] == 1
    assert counts["B0899JMQ7F"] == 0  # 記事として存在するが被参照は 0
    assert coverage["total_articles"] == 2
    assert coverage["sidecars_with_suggestions"] == 1
    assert coverage["suggestion_total"] == 1
    assert coverage["distinct_target_asins"] == 1
    assert coverage["coverage_ratio"] == 0.5  # 1/2 は閾値 10% を上回る
    assert coverage["low_coverage"] is False


def test_count_inbound_suggestions_flags_low_coverage(tmp_path):
    articles_dir = tmp_path / "articles"
    # 20 記事中 1 記事だけ suggestions あり (5%) -> 閾値 10% 未満
    for i in range(20):
        _write_article(articles_dir, f"2026-07-01-B{i:09d}")
    (articles_dir / "2026-07-01-B000000000.seo.json").write_text(
        json.dumps({"internal_link_suggestions": [
            {"target_asin": "B000000001", "anchor_text": "x"},
        ]}), encoding="utf-8",
    )
    counts, coverage, error = count_inbound_suggestions(articles_dir)
    assert error is None
    assert coverage["coverage_ratio"] == pytest.approx(0.05)
    assert coverage["low_coverage"] is True
    assert coverage["coverage_ratio"] < INBOUND_SUGGESTION_COVERAGE_LOW_THRESHOLD


def test_build_inbound_report_zero_becomes_unknown_under_low_coverage(census_fixture, tmp_path):
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")
    _write_article(articles_dir, "2026-07-01-B0BY2HLW37")
    counts, coverage, error = count_inbound_suggestions(articles_dir)
    assert error is None
    assert coverage["low_coverage"] is True  # 0/2 sidecar に suggestions あり
    urls, _ = extract_404_urls(census_fixture)
    report = build_inbound_report(urls, counts, coverage["low_coverage"])
    assert all(r["inbound_suggestions"] == "unknown" for r in report)


def test_build_inbound_report_keeps_real_number_when_coverage_sufficient(census_fixture, tmp_path):
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")
    _write_article(articles_dir, "2026-07-01-B0BY2HLW37")
    (articles_dir / "2026-07-01-B0899JMQ7F.seo.json").write_text(
        json.dumps({"internal_link_suggestions": [
            {"target_asin": "B0BY2HLW37", "anchor_text": "x"},
        ]}), encoding="utf-8",
    )
    counts, coverage, error = count_inbound_suggestions(articles_dir)
    assert error is None
    assert coverage["low_coverage"] is False  # 1/2 は閾値を上回る
    urls, _ = extract_404_urls(census_fixture)
    report = build_inbound_report(urls, counts, coverage["low_coverage"])
    by_asin = {r["asin"]: r["inbound_suggestions"] for r in report}
    assert by_asin["B0BY2HLW37"] == 1
    assert by_asin["B0899JMQ7F"] == 0  # 実測 0 (カバレッジ十分なので unknown にしない)


def test_build_inbound_report_unknown_when_counts_unavailable(census_fixture):
    urls, _ = extract_404_urls(census_fixture)
    report = build_inbound_report(urls, None, low_coverage=False)
    assert all(r["inbound_suggestions"] == "unknown" for r in report)


def test_build_inbound_report_positive_count_survives_low_coverage():
    """low_coverage=True でも実測値が 1 件以上あれば unknown に潰さない。"""
    urls = [{"url": "https://navi.omcha.jp/products/b0899jmq7f/"}]
    counts = {"B0899JMQ7F": 2}
    report = build_inbound_report(urls, counts, low_coverage=True)
    assert report[0]["inbound_suggestions"] == 2


def test_build_inbound_report_sorted_ascending_by_count():
    urls = [
        {"url": "https://navi.omcha.jp/products/b0899jmq7f/"},
        {"url": "https://navi.omcha.jp/products/b0by2hlw37/"},
    ]
    counts = {"B0899JMQ7F": 3, "B0BY2HLW37": 0}
    report = build_inbound_report(urls, counts, low_coverage=False)
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
    assert result["inbound_suggestions"] == []
    assert result["inbound_suggestions_error"] is None
    assert result["trend"] == []  # history 未作成


def test_run_missing_census_file_completes_normally(tmp_path):
    result = run(tmp_path / "nope.json", tmp_path / "history", tmp_path / "articles")
    assert result["count_404"] == 0
    assert result["inbound_suggestions_error"] is not None  # articles dir も無いので算出不能


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


# ---------------------------------------------------------------------------
# 一次ソース (data/site_audit/inbound_links.json) の優先 / フォールバック
# ---------------------------------------------------------------------------

def test_load_inbound_links_graph_missing_file_returns_none(tmp_path):
    assert load_inbound_links_graph(tmp_path / "nope.json") is None


def test_load_inbound_links_graph_malformed_json_returns_none(tmp_path):
    path = tmp_path / "inbound_links.json"
    path.write_text("not json", encoding="utf-8")
    assert load_inbound_links_graph(path) is None


def test_load_inbound_links_graph_missing_links_key_returns_none(tmp_path):
    path = tmp_path / "inbound_links.json"
    path.write_text(json.dumps({"generated_at": "2026-08-02T00:00:00Z"}), encoding="utf-8")
    assert load_inbound_links_graph(path) is None


def test_load_inbound_links_graph_valid_file_returns_dict(tmp_path):
    path = tmp_path / "inbound_links.json"
    payload = {
        "generated_at": "2026-08-02T00:00:00Z",
        "base_url": "https://navi.omcha.jp",
        "pages_crawled": 100,
        "sitemap_urls": 100,
        "sources_cap": 10,
        "links": {"https://navi.omcha.jp/products/b00005bhog/": {"inbound_count": 3, "sources": []}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    graph = load_inbound_links_graph(path)
    assert graph == payload


def test_build_inbound_report_from_graph_uses_inbound_count():
    urls = [
        {"url": "https://navi.omcha.jp/products/b00005bhog/"},
        {"url": "https://navi.omcha.jp/products/b0899jmq7f/"},
    ]
    graph = {
        "links": {
            "https://navi.omcha.jp/products/b00005bhog/": {"inbound_count": 5, "sources": []},
            "https://navi.omcha.jp/products/b0899jmq7f/": {"inbound_count": 0, "sources": []},
        }
    }
    report = build_inbound_report_from_graph(urls, graph)
    assert [r["inbound_suggestions"] for r in report] == [0, 5]


def test_build_inbound_report_from_graph_missing_url_is_unknown():
    urls = [{"url": "https://navi.omcha.jp/products/not-in-graph/"}]
    graph = {"links": {}}
    report = build_inbound_report_from_graph(urls, graph)
    assert report == [{
        "url": "https://navi.omcha.jp/products/not-in-graph/",
        "asin": None,
        "inbound_suggestions": "unknown",
    }]


def test_run_uses_primary_graph_when_present(tmp_path):
    census_path = tmp_path / "gsc_index_census.json"
    census_path.write_text(json.dumps({
        "fetched_at": "2026-07-26T23:08:05+00:00",
        "totals": {"sitemap_urls": 1, "inspected": 1, "indexed": 0, "not_indexed": 1, "errors": 0},
        "not_indexed_urls": [
            {
                "url": "https://navi.omcha.jp/products/b0899jmq7f/",
                "coverage_state": "見つかりませんでした（404）",
                "last_crawl_time": "2026-06-05T09:16:21Z",
                "google_canonical": "(none)",
            },
        ],
    }), encoding="utf-8")

    inbound_links_path = tmp_path / "inbound_links.json"
    inbound_links_path.write_text(json.dumps({
        "generated_at": "2026-08-02T00:00:00Z",
        "base_url": "https://navi.omcha.jp",
        "pages_crawled": 1900,
        "sitemap_urls": 1900,
        "sources_cap": 10,
        "links": {
            "https://navi.omcha.jp/products/b0899jmq7f/": {"inbound_count": 2, "sources": ["https://navi.omcha.jp/"]},
        },
    }), encoding="utf-8")

    result = run(census_path, tmp_path / "history", tmp_path / "articles", inbound_links_path)
    assert result["inbound_source"] == INBOUND_SOURCE_PRIMARY_GRAPH
    assert result["inbound_suggestions"] == [{
        "url": "https://navi.omcha.jp/products/b0899jmq7f/",
        "asin": "B0899JMQ7F",
        "inbound_suggestions": 2,
    }]
    assert result["inbound_suggestions_coverage"]["sitemap_urls"] == 1900
    assert result["inbound_suggestions_coverage"]["sources_cap"] == 10


def test_run_falls_back_to_sidecar_when_graph_absent(tmp_path):
    census_path = tmp_path / "gsc_index_census.json"
    census_path.write_text(json.dumps({
        "fetched_at": "2026-07-26T23:08:05+00:00",
        "totals": {"sitemap_urls": 10, "inspected": 10, "indexed": 10, "not_indexed": 0, "errors": 0},
        "not_indexed_urls": [],
    }), encoding="utf-8")
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "2026-07-01-B0899JMQ7F")

    # inbound_links.json をあえて作らない (マージ直後を模す)
    absent_path = tmp_path / "inbound_links.json"
    result = run(census_path, tmp_path / "history", articles_dir, absent_path)
    assert result["inbound_source"] == INBOUND_SOURCE_SIDECAR


def test_render_report_shows_primary_graph_source_note():
    text = render_report(
        trend=[], urls=[], unknown_states={}, crawl_distribution={},
        inbound_report=[], inbound_error=None, inbound_coverage={},
        inbound_source=INBOUND_SOURCE_PRIMARY_GRAPH,
    )
    assert "data/site_audit/inbound_links.json" in text
    assert "実クロールのリンクグラフ" in text


def test_render_report_shows_sidecar_source_note():
    text = render_report(
        trend=[], urls=[], unknown_states={}, crawl_distribution={},
        inbound_report=[], inbound_error=None, inbound_coverage={},
        inbound_source=INBOUND_SOURCE_SIDECAR,
    )
    assert "sidecar" in text
    assert "internal_link_suggestions" in text
