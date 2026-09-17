"""scripts/extract_first_party_experience.py unit tests (omcha-ops#264 C)。

ネットワークも Ollama も一切叩かない (requests.Session を fake、fetch_post_content を
monkeypatch、sleeper を注入)。
"""
from __future__ import annotations

import json
import pathlib

import pytest
import requests

from scripts.extract_first_party_experience import (
    build_post_id_by_url,
    build_targets,
    extract_asin_experience,
    extract_first_party_snippets,
    run,
    snippet_is_grounded,
)


class _FakeResponse:
    def __init__(self, json_body=None, status=200):
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeSession:
    """post() をキューから返す fake session (test_mine_experience.py と同型)。"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])

    def _next(self):
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def post(self, url, json=None, timeout=None):
        return self._next()

    def get(self, url, headers=None, params=None, timeout=None):
        return self._next()


def _no_sleep(_seconds):
    pass


# --------------------------------------------------------------------------
# snippet_is_grounded / 正規化
# --------------------------------------------------------------------------

def test_snippet_is_grounded_exact_substring():
    assert snippet_is_grounded("実際に使ってみて驚いた", "前置き 実際に使ってみて驚いた 後書き")


def test_snippet_is_grounded_normalizes_whitespace_and_width():
    # 本文側は改行入り・snippet 側は全角スペース混じり -> 正規化後は一致するはず
    source = "実際に\n使って\nみて驚いた"
    snippet = "実際に 使って　みて驚いた"
    assert snippet_is_grounded(snippet, source)


def test_snippet_is_grounded_false_when_paraphrased():
    assert not snippet_is_grounded("とても良い商品だった", "実際に使ってみて驚いた")


def test_snippet_is_grounded_false_for_empty_text():
    assert not snippet_is_grounded("", "本文")


# --------------------------------------------------------------------------
# build_targets / build_post_id_by_url
# --------------------------------------------------------------------------

def _source(asin, role, post_url, *, has_article=True, has_experience=False):
    return {
        "asin": asin, "role": role, "role_reason": "title_match",
        "post_url": post_url, "post_title": "タイトル",
        "fp_markers": 1, "own_images": 0, "in_corpus": True,
        "has_article": has_article, "has_experience": has_experience,
    }


def test_build_targets_filters_has_article_and_has_experience():
    data = {"sources": [
        _source("B0PRIMARY01", "primary", "https://omcha.jp/a/"),
        _source("B0DONE000001", "primary", "https://omcha.jp/b/", has_experience=True),
        _source("B0NOARTICLE1", "primary", "https://omcha.jp/c/", has_article=False),
    ]}
    targets = build_targets(data)
    assert list(targets.keys()) == ["B0PRIMARY01"]
    assert len(targets["B0PRIMARY01"]) == 1


def test_build_targets_groups_multiple_posts_per_asin():
    data = {"sources": [
        _source("B0PRIMARY01", "primary", "https://omcha.jp/a/"),
        _source("B0PRIMARY01", "compared", "https://omcha.jp/b/"),
    ]}
    targets = build_targets(data)
    assert len(targets["B0PRIMARY01"]) == 2


def test_build_post_id_by_url():
    data = {"posts_cache": {
        "101": {"link": "https://omcha.jp/a/", "title": "t"},
        "102": {"link": "https://omcha.jp/b/", "title": "t2"},
    }}
    assert build_post_id_by_url(data) == {"https://omcha.jp/a/": 101, "https://omcha.jp/b/": 102}


# --------------------------------------------------------------------------
# extract_first_party_snippets: gemma 呼び出し
# --------------------------------------------------------------------------

def test_extract_first_party_snippets_success():
    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "実際に使ってみた", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])
    out = extract_first_party_snippets("本文", "商品名", "ブランド", "http://ollama", "gemma4",
                                        session, sleeper=_no_sleep)
    assert out == [{"aspect": "体験談", "text": "実際に使ってみた", "confidence": "high"}]


def test_extract_first_party_snippets_not_entailed_returns_empty():
    inner = json.dumps({"entailed": False, "snippets": []})
    session = _FakeSession([_FakeResponse({"response": inner})])
    out = extract_first_party_snippets("無関係本文", "商品名", "ブランド", "http://ollama", "gemma4",
                                        session, sleeper=_no_sleep)
    assert out == []


def test_extract_first_party_snippets_empty_text_returns_empty():
    out = extract_first_party_snippets("", "商品名", "ブランド", "http://ollama", "gemma4",
                                        _FakeSession([]), sleeper=_no_sleep)
    assert out == []


def test_extract_first_party_snippets_gives_up_after_retries():
    session = _FakeSession([
        requests.ConnectionError("boom1"),
        requests.ConnectionError("boom2"),
        requests.ConnectionError("boom3"),
    ])
    out = extract_first_party_snippets("本文", "商品名", "ブランド", "http://ollama", "gemma4",
                                        session, sleeper=_no_sleep)
    assert out == []


# --------------------------------------------------------------------------
# extract_asin_experience: 役割フィルタ + 捏造ゲート
# --------------------------------------------------------------------------

def test_extract_asin_experience_role_primary_keeps_non_comparison_aspect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "うちの子は実際に使って喜んだ", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])

    def fake_fetch(post_id, wp_base_url, session_, sleeper=None):
        return "<p>本文: うちの子は実際に使って喜んだ、というエピソードです。</p>"

    monkeypatch.setattr("scripts.extract_first_party_experience.fetch_post_content", fake_fetch)

    records = [{"asin": "B0PRIMARY01", "role": "primary", "post_url": "https://omcha.jp/a/"}]
    post_id_by_url = {"https://omcha.jp/a/": 101}

    payload, stats = extract_asin_experience(
        "B0PRIMARY01", records, post_id_by_url,
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
    )
    assert payload is not None
    assert len(payload["snippets"]) == 1
    snippet = payload["snippets"][0]
    assert snippet["aspect"] == "体験談"
    assert snippet["source_type"] == "first_party"
    assert snippet["usable_as"] == "quote"
    assert snippet["source_url"] == "https://omcha.jp/a/"
    assert stats == {"posts": 1, "checked": 1, "role_filtered": 0, "fabrication_discarded": 0, "kept": 1}


def test_extract_asin_experience_role_compared_drops_non_comparison_aspect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0COMPARED1", "title": "比較対象商品 テストブランド"},
    ]}), encoding="utf-8")

    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "実際に使って良かった", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])

    def fake_fetch(post_id, wp_base_url, session_, sleeper=None):
        return "<p>実際に使って良かった、という本文です。</p>"

    monkeypatch.setattr("scripts.extract_first_party_experience.fetch_post_content", fake_fetch)

    records = [{"asin": "B0COMPARED1", "role": "compared", "post_url": "https://omcha.jp/a/"}]
    post_id_by_url = {"https://omcha.jp/a/": 101}

    payload, stats = extract_asin_experience(
        "B0COMPARED1", records, post_id_by_url,
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
    )
    # compared は「比較」以外の aspect を捨てるので snippet は残らない
    assert payload is None
    assert stats["role_filtered"] == 1
    assert stats["kept"] == 0


def test_extract_asin_experience_role_compared_keeps_comparison_aspect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0COMPARED1", "title": "比較対象商品 テストブランド"},
    ]}), encoding="utf-8")

    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "比較", "text": "こちらと比べると軽い", "confidence": "medium"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])

    def fake_fetch(post_id, wp_base_url, session_, sleeper=None):
        return "<p>こちらと比べると軽い、という本文です。</p>"

    monkeypatch.setattr("scripts.extract_first_party_experience.fetch_post_content", fake_fetch)

    records = [{"asin": "B0COMPARED1", "role": "compared", "post_url": "https://omcha.jp/a/"}]
    post_id_by_url = {"https://omcha.jp/a/": 101}

    payload, stats = extract_asin_experience(
        "B0COMPARED1", records, post_id_by_url,
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
    )
    assert payload is not None
    assert payload["snippets"][0]["aspect"] == "比較"
    assert stats["role_filtered"] == 0
    assert stats["kept"] == 1


def test_extract_asin_experience_fabrication_gate_discards_ungrounded_snippet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    inner = json.dumps({
        "entailed": True,
        # gemma が本文に無い文言を作文したケース (捏造)
        "snippets": [{"aspect": "体験談", "text": "存在しない架空の体験談です", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])

    def fake_fetch(post_id, wp_base_url, session_, sleeper=None):
        return "<p>実際に使ってみたという本文です。</p>"

    monkeypatch.setattr("scripts.extract_first_party_experience.fetch_post_content", fake_fetch)

    records = [{"asin": "B0PRIMARY01", "role": "primary", "post_url": "https://omcha.jp/a/"}]
    post_id_by_url = {"https://omcha.jp/a/": 101}

    payload, stats = extract_asin_experience(
        "B0PRIMARY01", records, post_id_by_url,
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
    )
    assert payload is None
    assert stats["fabrication_discarded"] == 1
    assert stats["kept"] == 0


def test_extract_asin_experience_missing_amazon_item_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": []}), encoding="utf-8")

    payload, stats = extract_asin_experience(
        "B0NOTFOUND1", [{"asin": "B0NOTFOUND1", "role": "primary", "post_url": "https://omcha.jp/a/"}],
        {"https://omcha.jp/a/": 101},
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=_FakeSession([]), sleeper=_no_sleep,
    )
    assert payload is None
    assert stats["posts"] == 0


def test_extract_asin_experience_missing_post_id_is_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    payload, stats = extract_asin_experience(
        "B0PRIMARY01",
        [{"asin": "B0PRIMARY01", "role": "primary", "post_url": "https://omcha.jp/unknown/"}],
        post_id_by_url={},  # 対応する post_id が無い
        wp_base_url="https://omcha.jp", per_asin_dir=tmp_path / "data" / "raw" / "per_asin",
        ollama_url="http://ollama", model="gemma4", session=_FakeSession([]), sleeper=_no_sleep,
    )
    assert payload is None
    assert stats["posts"] == 0


# --------------------------------------------------------------------------
# run(): 対象選定・既存ファイルの不上書き・dry-run・limit/asins
# --------------------------------------------------------------------------

def _write_sources_fixture(path: pathlib.Path, sources: list[dict], posts_cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"posts_cache": posts_cache, "sources": sources}), encoding="utf-8")


def test_run_writes_experience_for_new_asin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    sources_path = tmp_path / "data" / "analytics" / "first_party_sources.json"
    _write_sources_fixture(
        sources_path,
        [_source("B0PRIMARY01", "primary", "https://omcha.jp/a/")],
        {"101": {"link": "https://omcha.jp/a/", "title": "t"}},
    )

    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "実際に使ってみた", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])

    def fake_fetch(post_id, wp_base_url, session_, sleeper=None):
        return "<p>実際に使ってみた、という本文です。</p>"

    monkeypatch.setattr("scripts.extract_first_party_experience.fetch_post_content", fake_fetch)

    per_asin_dir = tmp_path / "data" / "raw" / "per_asin"
    summary = run(
        sources_path=sources_path, per_asin_dir=per_asin_dir,
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
    )
    assert summary["written"] == 1
    out_path = per_asin_dir / "B0PRIMARY01" / "experience.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["snippets"][0]["source_type"] == "first_party"


def test_run_does_not_overwrite_existing_experience_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    sources_path = tmp_path / "data" / "analytics" / "first_party_sources.json"
    # has_experience=False (A の出力が古い) だが、実ファイルは既に存在するケース
    _write_sources_fixture(
        sources_path,
        [_source("B0PRIMARY01", "primary", "https://omcha.jp/a/", has_experience=False)],
        {"101": {"link": "https://omcha.jp/a/", "title": "t"}},
    )

    per_asin_dir = tmp_path / "data" / "raw" / "per_asin"
    existing_dir = per_asin_dir / "B0PRIMARY01"
    existing_dir.mkdir(parents=True)
    existing_payload = {"asin": "B0PRIMARY01", "snippets": [{"source_type": "third_party"}]}
    (existing_dir / "experience.json").write_text(json.dumps(existing_payload), encoding="utf-8")

    summary = run(
        sources_path=sources_path, per_asin_dir=per_asin_dir,
        ollama_url="http://ollama", model="gemma4", session=_FakeSession([]), sleeper=_no_sleep,
    )
    assert summary["written"] == 0
    # 既存ファイルは変更されていない (第三者素材が消えていない)
    assert json.loads((existing_dir / "experience.json").read_text(encoding="utf-8")) == existing_payload


def test_run_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")

    sources_path = tmp_path / "data" / "analytics" / "first_party_sources.json"
    _write_sources_fixture(
        sources_path,
        [_source("B0PRIMARY01", "primary", "https://omcha.jp/a/")],
        {"101": {"link": "https://omcha.jp/a/", "title": "t"}},
    )

    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "実際に使ってみた", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])
    monkeypatch.setattr(
        "scripts.extract_first_party_experience.fetch_post_content",
        lambda post_id, wp_base_url, session_, sleeper=None: "<p>実際に使ってみた、という本文です。</p>",
    )

    per_asin_dir = tmp_path / "data" / "raw" / "per_asin"
    summary = run(
        sources_path=sources_path, per_asin_dir=per_asin_dir,
        ollama_url="http://ollama", model="gemma4", session=session, sleeper=_no_sleep,
        dry_run=True,
    )
    assert summary["written"] == 0
    assert not (per_asin_dir / "B0PRIMARY01" / "experience.json").exists()


def test_run_missing_sources_file_returns_empty(tmp_path):
    summary = run(sources_path=tmp_path / "does-not-exist.json", session=_FakeSession([]))
    assert summary == {"processed": [], "written": 0}


def test_run_respects_asins_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0PRIMARY01", "title": "商品1 ブランド"},
        {"asin": "B0PRIMARY02", "title": "商品2 ブランド"},
    ]}), encoding="utf-8")

    sources_path = tmp_path / "data" / "analytics" / "first_party_sources.json"
    _write_sources_fixture(
        sources_path,
        [
            _source("B0PRIMARY01", "primary", "https://omcha.jp/a/"),
            _source("B0PRIMARY02", "primary", "https://omcha.jp/b/"),
        ],
        {
            "101": {"link": "https://omcha.jp/a/", "title": "t"},
            "102": {"link": "https://omcha.jp/b/", "title": "t2"},
        },
    )

    monkeypatch.setattr(
        "scripts.extract_first_party_experience.fetch_post_content",
        lambda post_id, wp_base_url, session_, sleeper=None: None,
    )

    per_asin_dir = tmp_path / "data" / "raw" / "per_asin"
    summary = run(
        sources_path=sources_path, per_asin_dir=per_asin_dir, asins=["B0PRIMARY02"],
        session=_FakeSession([]),
        sleeper=_no_sleep,
    )
    processed_asins = [p["asin"] for p in summary["processed"]]
    assert processed_asins == ["B0PRIMARY02"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
