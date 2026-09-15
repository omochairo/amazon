"""Tests for scripts/analyze_third_party_yield.py (#4841 V1)."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_third_party_yield import (  # noqa: E402
    bootstrap_category_ratio,
    build_report,
    classify_host,
    compute_corpus_distribution,
    compute_host_breakdown,
    compute_yield,
    compute_yield_per_asin,
    evaluate_v2_conditions,
    list_snippet_source_hosts,
    load_experience_snippets,
    load_ledger_tried_asins,
    load_third_party_sources,
    parse_fetch_failures,
    parse_mined_asins,
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


# --------------------------------------------------------------------------
# parse_mined_asins (#4841 母艦レビュー 追補①: run ログから分母を作る)
# --------------------------------------------------------------------------

def test_parse_mined_asins_written_and_zero_snippet(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "run.txt"
    log.write_text(
        "2026-09-14T21:20:00Z [INFO] B0000000A1: wrote data/raw/per_asin/B0000000A1/"
        "experience.json (2 snippets)\n"
        "2026-09-14T21:21:00Z [INFO] B0000000A2: 0 snippets — not written\n",
        encoding="utf-8",
    )
    assert parse_mined_asins([log]) == {"B0000000A1", "B0000000A2"}


def test_parse_mined_asins_ignores_yahoo_lane_wrote_lines(tmp_path: pathlib.Path) -> None:
    # crawl_yahoo_reviews.py も同じログファイルに書く。"ASIN: wrote ..." で始まるが
    # 末尾が "(api count=N, M review bodies)" で終わり、third_party の mine_asin が
    # 実際に呼ばれたことの証拠にはならない — 誤って分母に混ぜない (#4841 母艦レビュー
    # で fresh(<30d)/no jan_code が crawl_yahoo_reviews.py 由来だと判明したのと同じ罠)
    log = tmp_path / "run.txt"
    log.write_text(
        "2026-09-14T21:00:00Z [INFO] B0000000A3: wrote data/raw/per_asin/B0000000A3/"
        "yahoo_reviews.json (api count=1, 0 review bodies)\n"
        "2026-09-14T21:00:01Z [INFO] B0000000A3: fresh (<30d) skip\n",
        encoding="utf-8",
    )
    assert parse_mined_asins([log]) == set()


def test_parse_mined_asins_missing_file_is_skipped(tmp_path: pathlib.Path) -> None:
    assert parse_mined_asins([tmp_path / "missing.txt"]) == set()


# --------------------------------------------------------------------------
# build_report: 分母は ledger 任意 + run ログの mined ASIN との和集合
# --------------------------------------------------------------------------

def test_build_report_denominator_from_run_logs_without_ledger(tmp_path: pathlib.Path) -> None:
    # 分母を作る run ログの正規表現は 10 桁の ASIN 前提 (実際の ASIN 形式)
    base = tmp_path / "per_asin"
    _write(base / "B0000000A1" / "third_party_sources.json", {
        "asin": "B0000000A1",
        "sources": [{"url": "https://ameblo.jp/foo/1", "host": "ameblo.jp", "title": ""}],
    })
    log = tmp_path / "run.txt"
    log.write_text(
        "[INFO] B0000000A1: wrote data/raw/per_asin/B0000000A1/experience.json (1 snippets)\n",
        encoding="utf-8",
    )
    report = build_report(base=base, ledger_path=None, run_log_paths=[log])
    assert report["denominator"]["tried_asins"] == 1
    assert report["denominator"]["tried_asins_from_ledger"] == 0
    assert report["denominator"]["tried_asins_from_run_logs"] == 1


def test_build_report_denominator_unions_ledger_and_run_logs(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "per_asin"
    ledger_path = tmp_path / "mining_ledger.json"
    _write(ledger_path, {"version": 1, "asins": {"B0000000A1": {"last_attempt": "2026-09-14T21:00:00Z"}}})
    log = tmp_path / "run.txt"
    log.write_text(
        "[INFO] B0000000A2: 0 snippets — not written\n", encoding="utf-8",
    )
    report = build_report(base=base, ledger_path=ledger_path, run_log_paths=[log])
    assert report["denominator"]["tried_asins_list"] == ["B0000000A1", "B0000000A2"]


# --------------------------------------------------------------------------
# compute_yield: 取得成功 URL を分母にした列 (#4841 母艦レビュー 追補②)
# --------------------------------------------------------------------------

def test_compute_yield_adds_success_denominator_columns() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://yodobashi.com/1", "host": "yodobashi.com", "title": ""},
            {"url": "https://yodobashi.com/2", "host": "yodobashi.com", "title": ""},
        ],
    }
    failed_urls = {"https://yodobashi.com/2"}  # 2件中1件が取得失敗
    snippets_by_asin = {
        "B001": [{"aspect": "不満", "source_url": "https://yodobashi.com/1"}],
    }
    result = compute_yield({"B001"}, sources_by_asin, failed_urls, snippets_by_asin)
    assert result["ec"]["tried_urls"] == 2
    assert result["ec"]["fetch_success_urls"] == 1
    assert result["ec"]["snippet_per_tried_url"] == 0.5
    # 取得成功 1 件だけを分母にすると 1.0 (取得失敗を分母に含めない)
    assert result["ec"]["snippet_per_success_url"] == 1.0


def test_compute_yield_success_denominator_zero_when_all_fetch_failed() -> None:
    sources_by_asin = {"B001": [{"url": "https://ec.example/1", "host": "ec.example", "title": ""}]}
    result = compute_yield({"B001"}, sources_by_asin, {"https://ec.example/1"}, {})
    assert result["other"]["fetch_success_urls"] == 0
    assert result["other"]["snippet_per_success_url"] == 0.0


# --------------------------------------------------------------------------
# compute_host_breakdown (#4841 母艦レビュー 追補②: ec のホスト別失敗率)
# --------------------------------------------------------------------------

def test_compute_host_breakdown_groups_non_top_hosts_as_other() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://yodobashi.com/1", "host": "yodobashi.com", "title": ""},
            {"url": "https://yodobashi.com/2", "host": "yodobashi.com", "title": ""},
            {"url": "https://askul.co.jp/1", "host": "askul.co.jp", "title": ""},  # ec だが top_hosts 外
            {"url": "https://ameblo.jp/1", "host": "ameblo.jp", "title": ""},  # ec 以外は対象外
        ],
    }
    failed_urls = {"https://yodobashi.com/2"}
    result = compute_host_breakdown(
        {"B001"}, sources_by_asin, failed_urls,
        category="ec", top_hosts=("yodobashi.com", "biccamera.com"), other_label="other_ec",
    )
    assert result["yodobashi.com"] == {"tried": 2, "fetch_failed": 1, "fetch_success": 1}
    assert result["other_ec"] == {"tried": 1, "fetch_failed": 0, "fetch_success": 1}
    assert "biccamera.com" not in result  # tried 0 は出力しない
    assert "ameblo.jp" not in result


# --------------------------------------------------------------------------
# list_snippet_source_hosts (#4841 母艦レビュー 追補③)
# --------------------------------------------------------------------------

def test_list_snippet_source_hosts_sorted_by_count_with_category() -> None:
    sources_by_asin = {
        "B001": [
            {"url": "https://family-games.blog/1", "host": "family-games.blog", "title": ""},
            {"url": "https://ameblo.jp/1", "host": "ameblo.jp", "title": ""},
        ],
    }
    snippets_by_asin = {
        "B001": [
            {"aspect": "不満", "source_url": "https://family-games.blog/1"},
            {"aspect": "良い点", "source_url": "https://family-games.blog/1"},
            {"aspect": "不満", "source_url": "https://ameblo.jp/1"},
        ],
    }
    result = list_snippet_source_hosts({"B001"}, sources_by_asin, snippets_by_asin)
    assert result[0]["host"] == "family-games.blog"
    assert result[0]["category"] == "other"  # 独自ドメインの個人ブログは blog に分類されない
    assert result[0]["snippet_count"] == 2
    assert result[1]["host"] == "ameblo.jp"
    assert result[1]["category"] == "blog"


# --------------------------------------------------------------------------
# compute_yield_per_asin / bootstrap_category_ratio (#4841 母艦レビュー 追補④)
# --------------------------------------------------------------------------

def test_compute_yield_per_asin_breaks_down_by_asin_and_category() -> None:
    sources_by_asin = {
        "B001": [{"url": "https://ameblo.jp/1", "host": "ameblo.jp", "title": ""}],
        "B002": [{"url": "https://yodobashi.com/1", "host": "yodobashi.com", "title": ""}],
    }
    snippets_by_asin = {"B001": [{"aspect": "不満", "source_url": "https://ameblo.jp/1"}]}
    result = compute_yield_per_asin({"B001", "B002"}, sources_by_asin, set(), snippets_by_asin)
    assert result["B001"]["blog"] == {"tried": 1, "fetch_failed": 0, "snippet_count": 1}
    assert result["B002"]["ec"] == {"tried": 1, "fetch_failed": 0, "snippet_count": 0}


def test_bootstrap_category_ratio_empty_per_asin_returns_none() -> None:
    assert bootstrap_category_ratio({}) is None


def test_bootstrap_category_ratio_is_deterministic_with_fixed_seed() -> None:
    per_asin = {
        "B001": {"blog": {"tried": 3, "fetch_failed": 0, "snippet_count": 4},
                 "ec": {"tried": 10, "fetch_failed": 2, "snippet_count": 1}},
        "B002": {"blog": {"tried": 2, "fetch_failed": 0, "snippet_count": 3},
                 "ec": {"tried": 8, "fetch_failed": 1, "snippet_count": 1}},
        "B003": {"blog": {"tried": 1, "fetch_failed": 0, "snippet_count": 1},
                 "ec": {"tried": 5, "fetch_failed": 0, "snippet_count": 0}},
    }
    r1 = bootstrap_category_ratio(per_asin, n_iter=200, seed=4841)
    r2 = bootstrap_category_ratio(per_asin, n_iter=200, seed=4841)
    assert r1 == r2  # 同じ seed なら再現する
    assert r1["effective_iterations"] > 0
    assert r1["ci_low"] <= r1["ci_high"]
    assert r1["ci_low"] > 1.0  # blog が明確に ec を上回るデータなので下限も 1 超え


def test_bootstrap_category_ratio_no_effective_samples_when_denominator_always_zero() -> None:
    # ec 側が全 ASIN で取得成功 0 (fetch_failed == tried) だと比が定義できない
    per_asin = {
        "B001": {"blog": {"tried": 1, "fetch_failed": 0, "snippet_count": 1},
                 "ec": {"tried": 1, "fetch_failed": 1, "snippet_count": 0}},
    }
    result = bootstrap_category_ratio(per_asin, n_iter=50, seed=1)
    assert result["effective_iterations"] == 0
    assert result["ci_low"] is None
    assert result["ci_high"] is None


# --------------------------------------------------------------------------
# evaluate_v2_conditions (#4841 母艦レビュー: サンプル下限 + ブートストラップ CI を追加)
# --------------------------------------------------------------------------

def _corpus(blog=20, total=1000):
    return {"total_urls": total, "by_category": {"blog": blog}}


def test_evaluate_v2_conditions_inconclusive_when_sample_floor_not_met() -> None:
    yield_by_category = {
        "blog": {"tried_urls": 3, "snippet_per_success_url": 5.0},
        "ec": {"tried_urls": 100, "snippet_per_success_url": 0.1},
    }
    result = evaluate_v2_conditions(yield_by_category, _corpus(), {"ci_low": 2.0})
    assert result["decision"] == "v1_inconclusive"
    assert result["conditions"]["3_sample_floor_ge_10"] is False


def test_evaluate_v2_conditions_go_when_all_conditions_met() -> None:
    yield_by_category = {
        "blog": {"tried_urls": 12, "snippet_per_success_url": 1.0},
        "ec": {"tried_urls": 100, "snippet_per_success_url": 0.2},
    }
    result = evaluate_v2_conditions(yield_by_category, _corpus(blog=20, total=1000),
                                     {"ci_low": 1.5})
    assert result["decision"] == "v2_go"
    assert all(result["conditions"].values())


def test_evaluate_v2_conditions_no_go_when_bootstrap_ci_crosses_one() -> None:
    yield_by_category = {
        "blog": {"tried_urls": 12, "snippet_per_success_url": 1.0},
        "ec": {"tried_urls": 100, "snippet_per_success_url": 0.2},
    }
    result = evaluate_v2_conditions(yield_by_category, _corpus(blog=20, total=1000),
                                     {"ci_low": 0.8})
    assert result["decision"] == "v2_no_go"
    assert result["conditions"]["4_bootstrap_ci_low_gt_1"] is False


def test_evaluate_v2_conditions_no_go_when_share_too_high() -> None:
    yield_by_category = {
        "blog": {"tried_urls": 12, "snippet_per_success_url": 1.0},
        "ec": {"tried_urls": 100, "snippet_per_success_url": 0.2},
    }
    result = evaluate_v2_conditions(yield_by_category, _corpus(blog=150, total=1000),
                                     {"ci_low": 1.5})
    assert result["decision"] == "v2_no_go"
    assert result["conditions"]["2_share_lt_10pct"] is False
