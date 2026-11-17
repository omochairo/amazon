"""scripts/audit_query_entailment.py unit tests (#2995 消費側②)."""
from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest
import requests

from scripts.audit_query_entailment import (
    build_judge_text,
    extract_asin_from_page,
    find_latest_history_file,
    judge_query,
    main,
    run,
)


# --------------------------------------------------------------------------
# extract_asin_from_page
# --------------------------------------------------------------------------

def test_extract_asin_from_page_valid():
    assert extract_asin_from_page("https://navi.omcha.jp/products/b0fnm4y35g/") == "B0FNM4Y35G"


def test_extract_asin_from_page_no_trailing_slash():
    assert extract_asin_from_page("https://navi.omcha.jp/products/B0FNM4Y35G") == "B0FNM4Y35G"


def test_extract_asin_from_page_invalid_length_returns_none():
    assert extract_asin_from_page("https://navi.omcha.jp/products/short/") is None


def test_extract_asin_from_page_no_products_path_returns_none():
    assert extract_asin_from_page("https://navi.omcha.jp/brands/foo/") is None


def test_extract_asin_from_page_empty_returns_none():
    assert extract_asin_from_page("") is None


# --------------------------------------------------------------------------
# build_judge_text
# --------------------------------------------------------------------------

def test_build_judge_text_includes_all_fields():
    article = {
        "title": "商品タイトル",
        "meta_description": "説明文",
        "narrative": {"lead": "リード文", "safety_note": "安全メモ"},
        "product": {"name": "商品名", "features": ["特徴1", "特徴2"]},
        "technical_specs": {"other": ["対象年齢: 3才以上"]},
        "faq": [{"question": "何歳から?", "answer": "3歳からです"}],
    }
    text = build_judge_text(article)
    assert "商品タイトル" in text
    assert "説明文" in text
    assert "リード文" in text
    assert "安全メモ" in text
    assert "特徴1" in text and "特徴2" in text
    assert "対象年齢: 3才以上" in text
    assert "何歳から?" in text and "3歳からです" in text


def test_build_judge_text_handles_missing_fields():
    assert build_judge_text({}) == ""
    assert build_judge_text({"title": "だけ"}) == "だけ"


def test_build_judge_text_not_dict_returns_empty():
    assert build_judge_text(None) == ""
    assert build_judge_text("not a dict") == ""


def test_build_judge_text_truncates_to_max_len():
    article = {"title": "あ" * 20000}
    text = build_judge_text(article)
    assert len(text) == 8000


def test_build_judge_text_skips_malformed_faq_entries():
    article = {"faq": [{"question": "Q"}, "not a dict", {"question": "Q2", "answer": "A2"}]}
    text = build_judge_text(article)
    assert "Q2" in text and "A2" in text
    assert "Q:" in text  # only the complete entry rendered


# --------------------------------------------------------------------------
# find_latest_history_file
# --------------------------------------------------------------------------

def test_find_latest_history_file_picks_lexicographically_max(tmp_path: pathlib.Path):
    (tmp_path / "2026-W24.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-W28.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-W25.json").write_text("{}", encoding="utf-8")
    latest = find_latest_history_file(tmp_path)
    assert latest is not None
    assert latest.name == "2026-W28.json"


def test_find_latest_history_file_missing_dir_returns_none(tmp_path: pathlib.Path):
    assert find_latest_history_file(tmp_path / "nope") is None


def test_find_latest_history_file_empty_dir_returns_none(tmp_path: pathlib.Path):
    assert find_latest_history_file(tmp_path) is None


# --------------------------------------------------------------------------
# judge_query (requests mocked via a fake session)
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, json_body, status=200):
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_judge_query_parses_valid_response():
    inner = json.dumps({"answered": True, "coverage": 0.8, "missing_aspects": ["価格帯"], "note": "概ね網羅"})
    session = _FakeSession([_FakeResponse({"response": inner})])
    result = judge_query("article text", "何歳から", "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["answered"] is True
    assert result["coverage"] == 0.8
    assert result["missing_aspects"] == ["価格帯"]
    assert result["error"] is None


def test_judge_query_retries_then_succeeds():
    inner = json.dumps({"answered": False, "coverage": 0.2, "missing_aspects": [], "note": ""})
    session = _FakeSession([
        requests.ConnectionError("boom"),
        _FakeResponse({"response": inner}),
    ])
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is None
    assert result["answered"] is False
    assert session.calls == 2


def test_judge_query_gives_up_after_max_retries_and_returns_error():
    session = _FakeSession([
        requests.ConnectionError("boom1"),
        requests.ConnectionError("boom2"),
        requests.ConnectionError("boom3"),
        requests.ConnectionError("boom4"),
    ])
    waits = []
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=waits.append)
    assert result["error"] is not None
    assert result["answered"] is None
    assert result["coverage"] is None
    assert session.calls == 4
    assert waits == [5.0, 30.0, 60.0]


def test_judge_query_handles_malformed_json_response():
    session = _FakeSession([
        _FakeResponse({"response": "not valid json"}),
        _FakeResponse({"response": "still not json"}),
        _FakeResponse({"response": "broken again"}),
        _FakeResponse({"response": "nope"}),
    ])
    waits = []
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=waits.append)
    assert result["error"] is not None
    assert result["answered"] is None
    assert session.calls == 4
    assert waits == [5.0, 30.0, 60.0]


def test_judge_query_retries_http_500_with_backoff_schedule():
    inner = json.dumps({"answered": True, "coverage": 0.7, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([
        _FakeResponse({}, status=500),
        _FakeResponse({}, status=500),
        _FakeResponse({"response": inner, "done_reason": "stop"}),
    ])
    waits = []
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=waits.append)
    assert result["error"] is None
    assert result["answered"] is True
    assert session.calls == 3
    assert waits == [5.0, 30.0]


def test_judge_query_retries_incomplete_done_reason():
    inner = json.dumps({"answered": True, "coverage": 0.9, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([
        _FakeResponse({"response": inner, "done_reason": "length"}),
        _FakeResponse({"response": inner, "done_reason": "stop"}),
    ])
    waits = []
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=waits.append)
    assert result["error"] is None
    assert result["answered"] is True
    assert session.calls == 2
    assert waits == [5.0]


def test_judge_query_gives_up_when_done_reason_never_stops():
    inner = json.dumps({"answered": True, "coverage": 0.9, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([_FakeResponse({"response": inner, "done_reason": "length"})] * 4)
    waits = []
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=waits.append)
    assert result["error"] is not None
    assert "done_reason=length" in result["error"]
    assert result["answered"] is None
    assert session.calls == 4
    assert waits == [5.0, 30.0, 60.0]


def test_judge_query_resolves_default_sleeper_at_call_time():
    inner = json.dumps({"answered": True, "coverage": 0.5, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([
        requests.ConnectionError("boom"),
        _FakeResponse({"response": inner, "done_reason": "stop"}),
    ])
    waits = []
    with mock.patch("scripts.audit_query_entailment.time.sleep", side_effect=waits.append):
        result = judge_query("text", "query", "http://ollama", "gemma4", session)
    assert result["error"] is None
    assert session.calls == 2
    assert waits == [5.0]


# --------------------------------------------------------------------------
# run() end-to-end with fake session + tmp dirs
# --------------------------------------------------------------------------

def test_run_end_to_end_writes_expected_payload(tmp_path: pathlib.Path):
    history_dir = tmp_path / "gsc_history"
    articles_dir = tmp_path / "articles"
    history_dir.mkdir()
    articles_dir.mkdir()

    gsc = {
        # baseline_ctr = 600/10000 = 0.06, threshold = max(0.06*0.5, 0.005) = 0.03.
        # page ctr 0.0283 < threshold なので A-1 で検出される。
        "totals": {"clicks_sum": 600, "impressions_sum": 10000},
        "by_page": [
            {"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "clicks": 3, "impressions": 106,
             "ctr": 0.0283, "position": 8.5},
        ],
        "by_combo": [
            {"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "query": "何歳から",
             "clicks": 2, "impressions": 26, "ctr": 0.0769, "position": 6.7},
        ],
        "range": {"start": "2026-07-02", "end": "2026-07-09"},
    }
    (history_dir / "2026-W28.json").write_text(json.dumps(gsc), encoding="utf-8")

    article = {"title": "テスト商品", "narrative": {"lead": "リード"}}
    (articles_dir / "2026-05-11-B0FNM4Y35G.json").write_text(
        json.dumps(article, ensure_ascii=False), encoding="utf-8"
    )

    inner = json.dumps({"answered": True, "coverage": 0.9, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([_FakeResponse({"response": inner})])

    out_path = tmp_path / "out" / "answerability_audit.json"
    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is True
    assert result["pages"] == 1
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source_week"] == "2026-W28"
    assert len(payload["pages"]) == 1
    page = payload["pages"][0]
    assert page["asin"] == "B0FNM4Y35G"
    assert page["queries"][0]["query"] == "何歳から"
    assert page["queries"][0]["answered"] is True


def test_run_all_judge_calls_failed_keeps_previous_output(tmp_path: pathlib.Path):
    # Ollama 停止等で全クエリが失敗したとき、前回の有効な結果を全件 null で上書きしない (#11004)
    history_dir = tmp_path / "gsc_history"
    articles_dir = tmp_path / "articles"
    history_dir.mkdir()
    articles_dir.mkdir()
    gsc = {
        "totals": {"clicks_sum": 600, "impressions_sum": 10000},
        "by_page": [{"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "clicks": 3,
                     "impressions": 106, "ctr": 0.0283, "position": 8.5}],
        "by_combo": [{"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "query": "何歳から",
                      "clicks": 2, "impressions": 26, "ctr": 0.0769, "position": 6.7}],
        "range": {"start": "2026-07-02", "end": "2026-07-09"},
    }
    (history_dir / "2026-W28.json").write_text(json.dumps(gsc), encoding="utf-8")
    (articles_dir / "2026-05-11-B0FNM4Y35G.json").write_text(
        json.dumps({"title": "テスト商品", "narrative": {"lead": "リード"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    out_path = tmp_path / "out.json"
    out_path.write_text('{"previous": true}', encoding="utf-8")

    bad = _FakeResponse({"response": "not json"})
    result = run(
        history_dir=history_dir, articles_dir=articles_dir, out_path=out_path,
        session=_FakeSession([bad, bad, bad, bad, bad, bad]), sleeper=lambda s: None,
    )
    assert result["written"] is False
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_run_no_detected_pages_does_not_write(tmp_path: pathlib.Path):
    history_dir = tmp_path / "gsc_history"
    articles_dir = tmp_path / "articles"
    history_dir.mkdir()
    articles_dir.mkdir()

    gsc = {
        "totals": {"clicks_sum": 0, "impressions_sum": 0},
        "by_page": [],
        "by_combo": [],
        "range": {},
    }
    (history_dir / "2026-W28.json").write_text(json.dumps(gsc), encoding="utf-8")

    out_path = tmp_path / "out.json"
    result = run(history_dir=history_dir, articles_dir=articles_dir, out_path=out_path)
    assert result["written"] is False
    assert not out_path.exists()


def test_run_no_history_file_does_not_write(tmp_path: pathlib.Path):
    out_path = tmp_path / "out.json"
    result = run(
        history_dir=tmp_path / "nope",
        articles_dir=tmp_path,
        out_path=out_path,
    )
    assert result["written"] is False


def test_run_resolves_default_sleeper_at_call_time(tmp_path: pathlib.Path):
    history_dir = tmp_path / "gsc_history"
    articles_dir = tmp_path / "articles"
    history_dir.mkdir()
    articles_dir.mkdir()

    gsc = {
        "totals": {"clicks_sum": 600, "impressions_sum": 10000},
        "by_page": [{"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "clicks": 3,
                     "impressions": 106, "ctr": 0.0283, "position": 8.5}],
        "by_combo": [{"page": "https://navi.omcha.jp/products/b0fnm4y35g/", "query": "何歳から",
                      "clicks": 2, "impressions": 26, "ctr": 0.0769, "position": 6.7}],
        "range": {"start": "2026-07-02", "end": "2026-07-09"},
    }
    (history_dir / "2026-W28.json").write_text(json.dumps(gsc), encoding="utf-8")
    (articles_dir / "2026-05-11-B0FNM4Y35G.json").write_text(
        json.dumps({"title": "テスト商品", "narrative": {"lead": "リード"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    inner = json.dumps({"answered": True, "coverage": 0.9, "missing_aspects": [], "note": "ok"})
    session = _FakeSession([
        _FakeResponse({}, status=500),
        _FakeResponse({"response": inner, "done_reason": "stop"}),
    ])
    waits = []
    out_path = tmp_path / "out.json"
    with mock.patch("scripts.audit_query_entailment.time.sleep", side_effect=waits.append):
        result = run(
            history_dir=history_dir,
            articles_dir=articles_dir,
            out_path=out_path,
            session=session,
        )
    assert result["written"] is True
    assert session.calls == 2
    assert waits == [5.0]


# --------------------------------------------------------------------------
# run() early abort on high failure rate
# --------------------------------------------------------------------------

def _setup_multi_query_env(tmp_path: pathlib.Path, page_query_counts: list[int]):
    history_dir = tmp_path / "gsc_history"
    articles_dir = tmp_path / "articles"
    history_dir.mkdir()
    articles_dir.mkdir()

    by_page = []
    by_combo = []
    for idx, q_count in enumerate(page_query_counts):
        asin = f"B0TEST000{idx + 1}"
        page_url = f"https://navi.omcha.jp/products/{asin.lower()}/"
        by_page.append({
            "page": page_url,
            "clicks": 3,
            "impressions": 200 - idx,
            "ctr": 0.015,
            "position": 8.5,
        })
        for q_idx in range(q_count):
            by_combo.append({
                "page": page_url,
                "query": f"query-{idx}-{q_idx}",
                "clicks": 1,
                "impressions": 20 - q_idx,
                "ctr": 0.05,
                "position": 7.0,
            })
        (articles_dir / f"2026-05-11-{asin}.json").write_text(
            json.dumps({"title": f"商品{idx}", "narrative": {"lead": "リード"}}, ensure_ascii=False),
            encoding="utf-8",
        )

    gsc = {
        "totals": {"clicks_sum": 600, "impressions_sum": 10000},
        "by_page": by_page,
        "by_combo": by_combo,
        "range": {"start": "2026-07-02", "end": "2026-07-09"},
    }
    (history_dir / "2026-W28.json").write_text(json.dumps(gsc), encoding="utf-8")

    out_path = tmp_path / "out.json"
    out_path.write_text('{"previous": true}', encoding="utf-8")
    return history_dir, articles_dir, out_path


def _ok_response() -> _FakeResponse:
    inner = json.dumps({"answered": True, "coverage": 0.8, "missing_aspects": [], "note": "ok"})
    return _FakeResponse({"response": inner, "done_reason": "stop"})


def test_run_aborts_early_when_all_queries_fail(tmp_path: pathlib.Path):
    # 総 10 件で 3 件目が失敗した時点 (3/10=30% > 20%) で残 7 件を打ち切る
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 5])
    session = _FakeSession([requests.ConnectionError("connection refused")] * 40)

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is False
    assert result["attempted"] == 3
    assert result["failed"] == 3
    assert result["aborted_remaining"] == 7
    assert session.calls == 12  # 3 queries * 4 attempts; remaining 7 queries are not attempted
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_run_20_queries_4_failures_completes_and_writes(tmp_path: pathlib.Path):
    # 総 20 件で失敗 4 件 (4/20=20% ちょうど) は打ち切らず完走して書き出す
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 5, 5, 5])
    http_500 = _FakeResponse({}, status=500)
    session = _FakeSession([http_500] * 16 + [_ok_response() for _ in range(16)])

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is True
    assert "aborted_remaining" not in result
    assert result["pages"] == 4
    assert session.calls == 32  # 4 failed queries * 4 attempts + 16 ok queries
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(payload["pages"]) == 4
    all_queries = [q for p in payload["pages"] for q in p["queries"]]
    assert len(all_queries) == 20
    assert sum(1 for q in all_queries if q["error"] is not None) == 4
    assert sum(1 for q in all_queries if q["error"] is None) == 16


def test_run_20_queries_5th_failure_aborts_immediately(tmp_path: pathlib.Path):
    # 総 20 件で 5 件目が失敗した時点 (5/20=25% > 20%) で残りを打ち切り書き込まない
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 5, 5, 5])
    fail = requests.ConnectionError("boom")
    session = _FakeSession(
        [fail] * 16
        + [_ok_response(), _ok_response(), _ok_response()]
        + [fail] * 4
        + [_ok_response() for _ in range(12)]
    )

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is False
    assert result["pages"] == 1
    assert result["attempted"] == 8
    assert result["failed"] == 5
    assert result["aborted_remaining"] == 12
    assert session.calls == 23  # 5 failed * 4 attempts + 3 ok
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_run_20_queries_aborts_even_when_5th_failure_is_last_query(tmp_path: pathlib.Path):
    # 総 20 件の最終クエリで 5 件目が失敗した場合 (5/20=25% > 20%) も書き込まない
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 5, 5, 5])
    fail = requests.ConnectionError("boom")
    session = _FakeSession([_ok_response() for _ in range(15)] + [fail] * 20)

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is False
    assert result["attempted"] == 20
    assert result["failed"] == 5
    assert result["aborted_remaining"] == 0
    assert session.calls == 35
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_run_single_query_failure_aborts_and_does_not_write(tmp_path: pathlib.Path):
    # 総 1 件で失敗 (1/1=100% > 20%) は打ち切り (書かない)
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [1])
    fail = requests.ConnectionError("boom")
    session = _FakeSession([fail] * 4)

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is False
    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert result["aborted_remaining"] == 0
    assert session.calls == 4
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_run_early_2_failures_completes_when_within_20_percent_of_total(tmp_path: pathlib.Path):
    # 2026-11-15 事象の再現: 序盤に 500 が 2 件連続しても総 20 件に対して 10% <= 20% なので完走して書く
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 5, 5, 5])
    http_500 = _FakeResponse({}, status=500)
    session = _FakeSession([http_500] * 8 + [_ok_response() for _ in range(18)])

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is True
    assert "aborted_remaining" not in result
    assert result["pages"] == 4
    assert session.calls == 26  # 2 failed * 4 attempts + 18 ok
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(payload["pages"]) == 4
    assert payload["pages"][0]["queries"][0]["error"] is not None
    assert payload["pages"][0]["queries"][1]["error"] is not None
    assert payload["pages"][0]["queries"][2]["error"] is None


def test_run_below_5_queries_aborts_immediately_when_exceeding_20_percent(tmp_path: pathlib.Path):
    # 総 4 件で 1 件目失敗 (1/4=25% > 20%) は即打ち切る (旧 _ABORT_MIN_ATTEMPTED 撤廃の確認)
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [4])
    fail = requests.ConnectionError("boom")
    session = _FakeSession([fail] * 4 + [_ok_response() for _ in range(3)])

    result = run(
        history_dir=history_dir,
        articles_dir=articles_dir,
        out_path=out_path,
        session=session,
        sleeper=lambda s: None,
    )

    assert result["written"] is False
    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert result["aborted_remaining"] == 3
    assert session.calls == 4
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'


def test_main_exits_1_when_aborted(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    history_dir, articles_dir, out_path = _setup_multi_query_env(tmp_path, [5, 1])
    session = _FakeSession([requests.ConnectionError("down")] * 24)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_query_entailment.py",
            "--history-dir", str(history_dir),
            "--articles-dir", str(articles_dir),
            "--out", str(out_path),
        ],
    )
    with (
        mock.patch("scripts.audit_query_entailment.requests.Session", return_value=session),
        mock.patch("scripts.audit_query_entailment.time.sleep"),
    ):
        exit_code = main()

    assert exit_code == 1
    assert session.calls == 8  # 6 queries total: aborts on 2nd failure (2/6=33.3% > 20%)
    assert out_path.read_text(encoding="utf-8") == '{"previous": true}'
