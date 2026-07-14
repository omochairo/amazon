"""scripts/audit_query_entailment.py unit tests (#2995 消費側②)."""
from __future__ import annotations

import json
import pathlib

import pytest
import requests

from scripts.audit_query_entailment import (
    build_judge_text,
    extract_asin_from_page,
    find_latest_history_file,
    judge_query,
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

    def post(self, url, json=None, timeout=None):
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
    ])
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is not None
    assert result["answered"] is None
    assert result["coverage"] is None


def test_judge_query_handles_malformed_json_response():
    session = _FakeSession([
        _FakeResponse({"response": "not valid json"}),
        _FakeResponse({"response": "still not json"}),
        _FakeResponse({"response": "nope"}),
    ])
    result = judge_query("text", "query", "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is not None
    assert result["answered"] is None


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
