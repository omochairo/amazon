"""scripts/judge_ambiguous_products.py unit tests (#2686 案2)."""
from __future__ import annotations

import json
import pathlib

import pytest
import requests

from scripts.judge_ambiguous_products import (
    build_prompt,
    judge_ambiguous_query,
    load_ambiguous_targets,
    run,
)


# --------------------------------------------------------------------------
# fakes (audit_query_entailment のテストと同じスタイル)
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


def _probe_json_entry(**overrides):
    base = {
        "query": "リカちゃん人形", "raw_query": "リカちゃん 人形", "volume": 40500,
        "sites": ["a"], "error": None, "hits": 10, "genre_pass_hits": 8,
        "title_overlap": 0.5, "verdict": "ambiguous", "verdict_reason": "partial_title_overlap",
        "sample_titles": ["タカラトミー リカちゃん ドール ハウス おもちゃ"],
    }
    base.update(overrides)
    return base


def _write_probe_json(path, results):
    path.write_text(json.dumps({
        "generated_at": "2026-08-10T00:00:00Z",
        "params": {"limit": 200, "search_index": "Toys", "item_count": 10},
        "summary": {"keywords_probed": len(results)},
        "results": results,
    }, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# load_ambiguous_targets: ambiguous だけが対象になること
# --------------------------------------------------------------------------

def test_only_ambiguous_verdict_is_targeted(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [
        _probe_json_entry(query="ambiguous語", verdict="ambiguous"),
        _probe_json_entry(query="product語", verdict="product", verdict_reason="full_title_overlap"),
        _probe_json_entry(query="non_product語", verdict="non_product", verdict_reason="no_hits"),
    ])
    targets = load_ambiguous_targets(src, limit=0)
    assert [t["query"] for t in targets] == ["ambiguous語"]


def test_targets_sorted_by_volume_desc(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [
        _probe_json_entry(query="小さい", volume=10),
        _probe_json_entry(query="大きい", volume=99999),
    ])
    targets = load_ambiguous_targets(src, limit=0)
    assert [t["query"] for t in targets] == ["大きい", "小さい"]


def test_limit_caps_targets(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(query=f"語{i}", volume=100 - i) for i in range(5)])
    targets = load_ambiguous_targets(src, limit=2)
    assert len(targets) == 2


# --------------------------------------------------------------------------
# build_prompt: sample_titles がプロンプトに含まれる (Amazon API を呼ばない)
# --------------------------------------------------------------------------

def test_build_prompt_includes_query_and_sample_titles():
    prompt = build_prompt("リカちゃん人形", ["タカラトミー リカちゃん ドール ハウス おもちゃ"])
    assert "リカちゃん人形" in prompt
    assert "タカラトミー リカちゃん ドール ハウス おもちゃ" in prompt


def test_build_prompt_handles_empty_sample_titles():
    prompt = build_prompt("知育 村", [])
    assert "知育 村" in prompt
    assert "該当商品なし" in prompt


def test_prompt_forbids_using_hit_existence_as_evidence():
    """初版プロンプトの誤通過7件は「Amazon に商品がヒットするから商品クエリ」
    という循環論法だった (run 31394640877 実測)。ambiguous に回ってくる語は
    定義上すべて商品が返っているので、ヒットの有無は全語共通の定数であって
    証拠にならない。この禁止をプロンプトから落とすと同じ誤りが再発するため
    固定する。
    """
    prompt = build_prompt("すいちゃん みい つけた", ["みいつけた!マスコットぬいぐるみ スイちゃん"])
    assert "判断材料にしてはいけない" in prompt
    assert "クエリの語そのものが商品を指しているか" in prompt


def test_prompt_limits_character_name_rule_to_missing_product_type():
    """「キャラクター名単体」ルールの過剰適用で「ポケモングッズ」
    「トイストーリー ウッディー」「dレックス」を落としていた (実測)。
    商品種別を伴う語・実在商品名は product 側とみなす旨を固定する。
    """
    prompt = build_prompt("ポケモングッズ", ["シャイン NEW イーブイバンク"])
    assert "商品種別を伴わないキャラクター名" in prompt
    assert "実在する商品名・シリーズ名" in prompt


def test_build_prompt_truncates_to_max_titles():
    titles = [f"タイトル{i}" for i in range(15)]
    prompt = build_prompt("語", titles)
    for i in range(10):
        assert f"タイトル{i}" in prompt
    assert "タイトル10" not in prompt


# --------------------------------------------------------------------------
# judge_ambiguous_query: リトライ・エラー畳み込み
# --------------------------------------------------------------------------

def test_judge_ambiguous_query_parses_valid_response():
    inner = json.dumps({"is_product_query": True, "confidence": 0.9, "reason": "実在の商品タイトルと一致"})
    session = _FakeSession([_FakeResponse({"response": inner})])
    result = judge_ambiguous_query("リカちゃん人形", ["タカラトミー リカちゃん ドール"],
                                   "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["is_product_query"] is True
    assert result["confidence"] == 0.9
    assert result["error"] is None


def test_judge_ambiguous_query_retries_on_malformed_json_then_falls_to_error():
    """不正な JSON を返し続けたらリトライし、最終的に error に畳まれる。"""
    session = _FakeSession([
        _FakeResponse({"response": "not valid json"}),
        _FakeResponse({"response": "still not json"}),
        _FakeResponse({"response": "nope"}),
    ])
    result = judge_ambiguous_query("語", [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is not None
    assert result["is_product_query"] is None
    assert session.calls == 3


def test_judge_ambiguous_query_retries_then_succeeds():
    inner = json.dumps({"is_product_query": False, "confidence": 0.7, "reason": "施設名"})
    session = _FakeSession([
        requests.ConnectionError("boom"),
        _FakeResponse({"response": inner}),
    ])
    result = judge_ambiguous_query("語", [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is None
    assert result["is_product_query"] is False
    assert session.calls == 2


def test_judge_ambiguous_query_gives_up_after_max_retries():
    session = _FakeSession([
        requests.ConnectionError("boom1"),
        requests.ConnectionError("boom2"),
        requests.ConnectionError("boom3"),
    ])
    result = judge_ambiguous_query("語", [], "http://ollama", "gemma4", session, sleeper=lambda s: None)
    assert result["error"] is not None
    assert result["confidence"] is None


# --------------------------------------------------------------------------
# run(): 1件の失敗で全体を止めない / --dry-run / --limit / ネットワークに出ない
# --------------------------------------------------------------------------

def test_run_one_failure_does_not_stop_the_lane(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [
        _probe_json_entry(query="壊れる語", raw_query="壊れる 語"),
        _probe_json_entry(query="次の語", raw_query="次の 語"),
    ])
    ok_inner = json.dumps({"is_product_query": True, "confidence": 0.8, "reason": "一致"})
    session = _FakeSession([
        requests.ConnectionError("boom1"),
        requests.ConnectionError("boom2"),
        requests.ConnectionError("boom3"),
        _FakeResponse({"response": ok_inner}),
    ])
    out = tmp_path / "out.json"
    payload = run(src, out, limit=0, dry_run=False, session=session, sleeper=lambda s: None)
    assert len(payload["results"]) == 2
    failed = [r for r in payload["results"] if r["query"] == "壊れる語"][0]
    assert failed["error"] is not None
    ok = [r for r in payload["results"] if r["query"] == "次の語"][0]
    assert ok["error"] is None
    assert ok["is_product_query"] is True
    assert payload["summary"]["error_count"] == 1
    assert payload["summary"]["judged"] == 1


def test_dry_run_makes_no_http_calls(tmp_path: pathlib.Path, monkeypatch):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(query="ambiguous語")])

    calls = []
    monkeypatch.setattr(requests.Session, "post", lambda self, *a, **k: calls.append(1))

    out = tmp_path / "out.json"
    payload = run(src, out, limit=0, dry_run=True, sleeper=lambda s: None)
    assert calls == []
    assert not out.exists()
    assert payload["targets"][0]["query"] == "ambiguous語"


def test_limit_is_effective_in_run(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(query=f"語{i}", volume=100 - i) for i in range(5)])
    out = tmp_path / "out.json"
    payload = run(src, out, limit=0, dry_run=True, sleeper=lambda s: None)
    assert len(payload["targets"]) == 5

    payload_limited = run(src, out, limit=2, dry_run=True, sleeper=lambda s: None)
    assert len(payload_limited["targets"]) == 2


def test_run_never_calls_amazon_api(tmp_path: pathlib.Path, monkeypatch):
    """Amazon API (creators_api_client) を一切 import しないこと。"""
    import builtins
    orig_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "creators_api_client":
            raise AssertionError("creators_api_client must not be imported")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(query="語")])
    inner = json.dumps({"is_product_query": True, "confidence": 0.5, "reason": "ok"})
    session = _FakeSession([_FakeResponse({"response": inner})])
    run(src, tmp_path / "out.json", limit=0, dry_run=False, session=session, sleeper=lambda s: None)


def test_run_writes_expected_payload_shape(tmp_path: pathlib.Path):
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(query="リカちゃん人形", raw_query="リカちゃん 人形")])
    inner = json.dumps({"is_product_query": True, "confidence": 0.95, "reason": "ドール表記の同一商品"})
    session = _FakeSession([_FakeResponse({"response": inner})])
    out = tmp_path / "out" / "ubersuggest_llm_judge.json"
    run(src, out, limit=0, dry_run=False, session=session, sleeper=lambda s: None)

    written = json.loads(out.read_text(encoding="utf-8"))
    assert "generated_at" in written
    assert "params" in written
    r = written["results"][0]
    assert r["query"] == "リカちゃん人形"
    assert r["raw_query"] == "リカちゃん 人形"
    assert r["probe_verdict"] == "ambiguous"
    assert r["sample_titles"]
    assert r["is_product_query"] is True
    assert r["confidence"] == 0.95
