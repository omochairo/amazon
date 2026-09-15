"""Tests for scripts/analyze_third_party_yield.py (#4841 V1)."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_third_party_yield import (  # noqa: E402
    build_report,
    classify_host,
    compute_corpus_distribution,
    compute_yield,
    load_experience_snippets,
    load_ledger_tried_asins,
    load_third_party_sources,
    parse_fetch_failures,
    sample_js_shell_check,
    tried_urls_for_asin,
)


def _write(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# classify_host
# --------------------------------------------------------------------------

def test_classify_host_exact_and_suffix_match() -> None:
    assert classify_host("ameblo.jp") == "blog"
    assert classify_host("youtube.com") == "sns"
    assert classify_host("m.youtube.com") == "sns"  # サブドメイン (完全一致でない)
    assert classify_host("yodobashi.com") == "ec"
    assert classify_host("takaratomy.co.jp") == "maker"
    assert classify_host("beyblade.takaratomy.co.jp") == "maker"  # サブドメイン許容
    assert classify_host("tamagotchi.fandom.com") == "media"  # ファン wiki


def test_classify_host_unknown_falls_to_other() -> None:
    assert classify_host("some-tiny-toyshop-nobody-has-heard-of.jp") == "other"
    assert classify_host("") == "other"


def test_classify_host_does_not_over_match_substring() -> None:
    # "kakaku.com" のサフィックス一致であって、"notkakaku.com" のような無関係な
    # 実在ドメインまで拾わない (self_domain #6593 と同じ罠を作らない)
    assert classify_host("notkakaku.com") == "other"


# --------------------------------------------------------------------------
# ローダー
# --------------------------------------------------------------------------

def test_load_third_party_sources_reads_all_asins(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    _write(base / "B001" / "third_party_sources.json", {
        "asin": "B001",
        "sources": [
            {"url": "https://ameblo.jp/foo/entry-1.html", "host": "ameblo.jp", "title": "t1"},
            {"url": "https://yodobashi.com/product/1", "host": "yodobashi.com", "title": "t2"},
        ],
    })
    _write(base / "B002" / "third_party_sources.json", {"asin": "B002", "sources": []})
    result = load_third_party_sources(base)
    assert set(result.keys()) == {"B001"}  # 空 sources の ASIN は含めない
    assert len(result["B001"]) == 2


def test_load_experience_snippets(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    _write(base / "B001" / "experience.json", {
        "asin": "B001",
        "snippets": [
            {"aspect": "不満", "source_type": "blog", "source_url": "https://ameblo.jp/foo/entry-1.html"},
        ],
    })
    result = load_experience_snippets(base)
    assert len(result["B001"]) == 1
    assert result["B001"][0]["aspect"] == "不満"


def test_load_ledger_tried_asins(tmp_path: pathlib.Path) -> None:
    ledger_path = tmp_path / "mining_ledger.json"
    _write(ledger_path, {
        "version": 1,
        "asins": {
            "B001": {"last_attempt": "2026-09-14T21:00:00Z", "written": True, "snippets": 2},
            "B002": {"last_attempt": "2026-09-14T21:05:00Z", "written": False, "snippets": 0},
        },
    })
    assert load_ledger_tried_asins(ledger_path) == {"B001", "B002"}


def test_load_ledger_tried_asins_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    assert load_ledger_tried_asins(tmp_path / "missing.json") == set()


def test_parse_fetch_failures(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "run.txt"
    log.write_text(
        "2026-09-14T21:18:53Z [WARNING] third_party fetch failed for "
        "https://www.maruka.jp/toy/toys/detail?t=1&id=1039: 404 Client Error — skip\n"
        "2026-09-14T21:20:00Z [INFO] B001: wrote data/raw/per_asin/B001/experience.json (2 snippets)\n",
        encoding="utf-8",
    )
    failed = parse_fetch_failures([log])
    assert failed == {"https://www.maruka.jp/toy/toys/detail?t=1&id=1039"}


def test_parse_fetch_failures_merges_multiple_files(tmp_path: pathlib.Path) -> None:
    log1 = tmp_path / "run1.txt"
    log2 = tmp_path / "run2.txt"
    log1.write_text(
        "[WARNING] third_party fetch failed for https://a.example/1: err — skip\n", encoding="utf-8")
    log2.write_text(
        "[WARNING] third_party fetch failed for https://b.example/2: err — skip\n", encoding="utf-8")
    assert parse_fetch_failures([log1, log2]) == {"https://a.example/1", "https://b.example/2"}


def test_parse_fetch_failures_missing_file_is_skipped(tmp_path: pathlib.Path) -> None:
    assert parse_fetch_failures([tmp_path / "missing.txt"]) == set()


# --------------------------------------------------------------------------
# tried_urls_for_asin (検索結果ページの除外)
# --------------------------------------------------------------------------

def test_tried_urls_excludes_search_result_pages() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://search.kakaku.com/foo", "host": "search.kakaku.com", "title": ""},
            {"url": "https://ameblo.jp/foo/entry-1.html", "host": "ameblo.jp", "title": ""},
        ],
    }
    tried = tried_urls_for_asin("B001", sources_by_asin)
    assert [r["url"] for r in tried] == ["https://ameblo.jp/foo/entry-1.html"]


# --------------------------------------------------------------------------
# compute_corpus_distribution
# --------------------------------------------------------------------------

def test_compute_corpus_distribution_counts_and_reports_other() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""},
            {"url": "https://youtube.com/watch?v=1", "host": "youtube.com", "title": ""},
        ],
        "B002": [
            {"url": "https://unknown-toy-shop.example/x", "host": "unknown-toy-shop.example", "title": ""},
        ],
    }
    dist = compute_corpus_distribution(sources_by_asin)
    assert dist["total_urls"] == 3
    assert dist["by_category"] == {"blog": 1, "sns": 1, "other": 1}
    assert dist["top_other_hosts"] == [("unknown-toy-shop.example", 1)]


# --------------------------------------------------------------------------
# compute_yield (分母を tried_asins に絞った歩留まり)
# --------------------------------------------------------------------------

def test_compute_yield_restricts_to_tried_asins() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""},
            {"url": "https://ameblo.jp/foo/2", "host": "ameblo.jp", "title": ""},
        ],
        # B002 は third_party_sources.json は持つが ledger に無い (未試行) — 分母から除外
        "B002": [
            {"url": "https://ameblo.jp/foo/99", "host": "ameblo.jp", "title": ""},
        ],
    }
    failed_urls = {"https://ameblo.jp/foo/2"}
    snippets_by_asin = {
        "B001": [
            {"aspect": "不満", "source_url": "https://ameblo.jp/foo/1"},
            {"aspect": "体験談", "source_url": "https://ameblo.jp/foo/1"},
        ],
    }
    result = compute_yield({"B001"}, sources_by_asin, failed_urls, snippets_by_asin)
    assert result["blog"]["tried_urls"] == 2
    assert result["blog"]["fetch_failed"] == 1
    assert result["blog"]["urls_with_snippet"] == 1
    assert result["blog"]["snippet_count"] == 2
    assert result["blog"]["aspect_breakdown"] == {"不満": 1, "体験談": 1}
    assert result["blog"]["snippet_per_tried_url"] == 1.0


def test_compute_yield_ignores_snippet_source_url_outside_third_party_sources() -> None:
    sources_by_asin = {"B001": [{"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""}]}
    # news.json 由来など third_party_sources.json に無い source_url は集計対象外
    snippets_by_asin = {"B001": [{"aspect": "比較", "source_url": "https://news.example/1"}]}
    result = compute_yield({"B001"}, sources_by_asin, set(), snippets_by_asin)
    assert result["blog"]["snippet_count"] == 0
    assert "other" not in result or result.get("other", {}).get("snippet_count", 0) == 0


def test_compute_yield_empty_tried_asins_returns_empty() -> None:
    assert compute_yield(set(), {"B001": [{"url": "https://ameblo.jp/1", "host": "ameblo.jp", "title": ""}]},
                          set(), {}) == {}


# --------------------------------------------------------------------------
# sample_js_shell_check (ネットワークは fake session で差し替え)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, bodies: dict[str, str]) -> None:
        self._bodies = bodies

    def get(self, url, headers=None, timeout=None):
        if url not in self._bodies:
            raise __import__("requests").exceptions.ConnectionError("no route")
        return _FakeResponse(self._bodies[url])


def test_sample_js_shell_check_detects_thin_body(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    _write(base / "B001" / "amazon.json", {"item": {"asin": "B001", "title": "BRIO 木製レール 直線レール"}})
    sources_by_asin = {
        "B001": [
            {"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""},
            {"url": "https://ameblo.jp/foo/2", "host": "ameblo.jp", "title": ""},
        ],
    }
    bodies = {
        "https://ameblo.jp/foo/1": "<html><body>BRIO の木製レールを使ってみた感想です。</body></html>",
        "https://ameblo.jp/foo/2": "<html><body><div id='app'></div></body></html>",  # JS 枠だけ
    }
    result = sample_js_shell_check(
        sources_by_asin, {"B001"}, base=base, sample_size=2,
        session=_FakeSession(bodies), sleeper=lambda _s: None,
    )
    assert result["checked"] == 2
    assert result["thin_body_count"] == 1
    assert result["thin_body_rate"] == 0.5


def test_sample_js_shell_check_defaults_to_full_corpus_not_tried_subset(tmp_path: pathlib.Path) -> None:
    # asin_pool を省略すると、tried_asins (実測では数件) に縛られず corpus 全体の
    # blog URL から抽出する — 分母が小さすぎて測れなくなるのを避ける設計
    base = tmp_path / "per_asin"
    _write(base / "B001" / "amazon.json", {"item": {"asin": "B001", "title": "BRIO 木製レール"}})
    _write(base / "B002" / "amazon.json", {"item": {"asin": "B002", "title": "LEGO クラシック"}})
    sources_by_asin = {
        "B001": [{"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""}],
        "B002": [{"url": "https://ameblo.jp/foo/2", "host": "ameblo.jp", "title": ""}],
    }
    bodies = {
        "https://ameblo.jp/foo/1": "<html><body>BRIO のレビュー</body></html>",
        "https://ameblo.jp/foo/2": "<html><body>LEGO のレビュー</body></html>",
    }
    result = sample_js_shell_check(
        sources_by_asin, base=base, sample_size=10,
        session=_FakeSession(bodies), sleeper=lambda _s: None,
    )
    assert result["checked"] == 2  # B001 だけに絞られていれば 1 のはず


def test_sample_js_shell_check_handles_fetch_error(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    _write(base / "B001" / "amazon.json", {"item": {"asin": "B001", "title": "BRIO 木製レール"}})
    sources_by_asin = {"B001": [{"url": "https://ameblo.jp/dead", "host": "ameblo.jp", "title": ""}]}
    result = sample_js_shell_check(
        sources_by_asin, {"B001"}, base=base, sample_size=1,
        session=_FakeSession({}), sleeper=lambda _s: None,
    )
    assert result["samples"][0]["status"] == "fetch_error"
    assert result["checked"] == 0
    assert result["thin_body_rate"] is None


# --------------------------------------------------------------------------
# build_report (結線の確認)
# --------------------------------------------------------------------------

def test_build_report_end_to_end(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    _write(base / "B001" / "third_party_sources.json", {
        "asin": "B001",
        "sources": [{"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""}],
    })
    _write(base / "B001" / "experience.json", {
        "asin": "B001",
        "snippets": [{"aspect": "不満", "source_url": "https://ameblo.jp/foo/1"}],
    })
    ledger_path = tmp_path / "mining_ledger.json"
    _write(ledger_path, {"version": 1, "asins": {"B001": {"last_attempt": "2026-09-14T21:00:00Z"}}})
    log = tmp_path / "run.txt"
    log.write_text("no failures here\n", encoding="utf-8")

    report = build_report(base=base, ledger_path=ledger_path, run_log_paths=[log])
    assert report["denominator"]["tried_asins"] == 1
    assert report["corpus_distribution"]["total_urls"] == 1
    assert report["yield_by_host_category"]["blog"]["snippet_count"] == 1
    assert "js_shell_check" not in report
