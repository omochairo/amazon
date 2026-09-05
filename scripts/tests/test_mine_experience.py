"""scripts/mine_experience.py unit tests (#3203 Phase 2)."""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest
import requests

from scripts import mine_experience
from scripts.mine_experience import (
    _USABLE_AS_MAP,
    _yahoo_rating_stats,
    extract_snippets,
    gather_antigravity,
    gather_threads,
    gather_third_party,
    gather_yahoo_aggregate,
    mine_asin,
    resolve_product_identity,
    run,
    select_targets,
    write_experience,
)


class _FakeResponse:
    def __init__(self, json_body=None, status=200, text=""):
        self._json = json_body
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeSession:
    """post()/get() をキューから返す fake session (audit_query_entailment のテストと同型)。"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls = 0

    def _next(self):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def post(self, url, json=None, params=None, timeout=None):
        return self._next()

    def get(self, url, headers=None, params=None, timeout=None):
        return self._next()


# --------------------------------------------------------------------------
# select_targets
# --------------------------------------------------------------------------

def test_select_targets_merges_audit_and_rewrite_queue_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    audit_dir = tmp_path / "data" / "analytics"
    audit_dir.mkdir(parents=True)
    (audit_dir / "answerability_audit.json").write_text(
        json.dumps({"pages": [{"asin": "B0FNM4Y35G"}, {"asin": "B0AAAAAAAA"}]}), encoding="utf-8"
    )
    queue_dir = tmp_path / "data" / "rewrite_queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "1.json").write_text(json.dumps({"asin": "B0AAAAAAAA"}), encoding="utf-8")  # dup
    (queue_dir / "2.json").write_text(json.dumps({"asin": "B0BBBBBBBB"}), encoding="utf-8")

    targets = select_targets(
        limit=0,
        audit_path=audit_dir / "answerability_audit.json",
        rewrite_queue_dir=queue_dir,
    )
    assert targets == ["B0FNM4Y35G", "B0AAAAAAAA", "B0BBBBBBBB"]


def test_select_targets_appends_explicit_asins(tmp_path):
    targets = select_targets(
        limit=0, asins=["B0EXPLICI1", "B0EXPLICI1"],
        audit_path=tmp_path / "missing.json",
        rewrite_queue_dir=tmp_path / "missing_dir",
    )
    assert targets == ["B0EXPLICI1"]


def test_select_targets_respects_limit(tmp_path):
    targets = select_targets(
        limit=2, asins=["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC"],
        audit_path=tmp_path / "missing.json",
        rewrite_queue_dir=tmp_path / "missing_dir",
    )
    assert targets == ["B0AAAAAAAA", "B0BBBBBBBB"]


def test_select_targets_ignores_malformed_asin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    audit_dir = tmp_path / "data" / "analytics"
    audit_dir.mkdir(parents=True)
    (audit_dir / "answerability_audit.json").write_text(
        json.dumps({"pages": [{"asin": "not-an-asin"}, {"asin": "B0GOODGOOD"}]}), encoding="utf-8"
    )
    targets = select_targets(limit=0, audit_path=audit_dir / "answerability_audit.json",
                              rewrite_queue_dir=tmp_path / "missing_dir")
    assert targets == ["B0GOODGOOD"]


# --------------------------------------------------------------------------
# アダプタ: secret 無しで skip すること
# --------------------------------------------------------------------------

def test_gather_threads_skips_without_token(monkeypatch):
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    assert gather_threads("商品名", token="") == []


def test_gather_third_party_empty_when_no_sources(tmp_path):
    base = tmp_path / "per_asin"
    (base / "B0XXXXXXXX").mkdir(parents=True)
    assert gather_third_party("B0XXXXXXXX", base=base, session=_FakeSession([])) == []


def test_gather_third_party_skips_search_result_urls(tmp_path):
    # #5490 案B: 検索結果ページを fetch しても体験談は取れない。収集側は塞いだが、
    # それ以前の行が store に残っている (2026-08-20 実測 498 行)。外部への無駄な
    # リクエストになるので、ここで落とす。_FakeSession は空キューなので、
    # 1 件でも fetch しようとすれば IndexError で落ちる = 呼んでいないことの証明。
    base = tmp_path / "per_asin"
    (base / "B0XXXXXXXX").mkdir(parents=True)
    (base / "B0XXXXXXXX" / "third_party_sources.json").write_text(
        json.dumps({"sources": [
            {"url": "https://search.kakaku.com/gravitrax", "host": "search.kakaku.com"},
            {"url": "https://www.biccamera.com/bc/category?q=x", "host": "biccamera.com"},
        ]}),
        encoding="utf-8",
    )
    session = _FakeSession([])
    assert gather_third_party("B0XXXXXXXX", base=base, session=session) == []
    assert session.calls == 0


def test_gather_yahoo_aggregate_empty_when_raw_dir_missing(tmp_path):
    candidates, stats = gather_yahoo_aggregate("B0XXXXXXXX", raw_dir=tmp_path / "nope")
    assert candidates == []
    assert stats == {"count": 0, "average": 0.0, "distribution": {}}


# --------------------------------------------------------------------------
# gather_antigravity: dbus-run-session -- agy --print のヘッドレス実行
# --------------------------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gather_antigravity_builds_candidate_on_success(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout="口コミ要約テキスト\n")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    result = gather_antigravity("商品名", "ブランド")
    assert len(result) == 1
    assert result[0]["text"] == "口コミ要約テキスト"
    assert result[0]["source_type"] == "antigravity"
    assert result[0]["source_url"] == ""
    assert captured["cmd"][:3] == ["dbus-run-session", "--", "agy"]
    # #6539 の罠回避形: --model が先、prompt は --print= に添付
    assert captured["cmd"][3:5] == ["--model", mine_experience.DEFAULT_ANTIGRAVITY_MODEL]
    assert captured["cmd"][5].startswith("--print=")
    assert "商品名" in captured["cmd"][5]
    assert "ブランド" in captured["cmd"][5]


def test_build_antigravity_argv_puts_model_before_print():
    """`agy --print <prompt> --model X` は --model 以降を prompt に食われる (#6539)。

    draft_sns_reply.build_agy_argv と同じ形にそろっていることを固定する。
    """
    argv = mine_experience.build_antigravity_argv("要約して", "gemini-3.8-flash-medium")
    assert argv == ["agy", "--model", "gemini-3.8-flash-medium", "--print=要約して"]


def test_build_antigravity_argv_omits_model_when_none():
    assert mine_experience.build_antigravity_argv("要約して", None) == ["agy", "--print=要約して"]
    assert mine_experience.build_antigravity_argv("要約して", "") == ["agy", "--print=要約して"]


def test_gather_antigravity_model_is_pinned_not_cli_default():
    """--model 無し (= agy CLI の既定) に戻さない。

    既定モデルは agy の更新で黙って動き、`--output-format json` の応答にモデル名が
    入らないので本番からは何に乗っているか観測できない。実測 (bench_agy_model.py)
    で選んだ値をピンする。
    """
    assert mine_experience.DEFAULT_ANTIGRAVITY_MODEL


def test_gather_antigravity_honours_env_override(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    monkeypatch.setenv("ANTIGRAVITY_MODEL", "gemini-3.1-pro-high")
    gather_antigravity("商品名", "ブランド")
    assert "gemini-3.1-pro-high" in captured["cmd"]


def test_gather_antigravity_explicit_model_beats_env(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="ok")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    monkeypatch.setenv("ANTIGRAVITY_MODEL", "gemini-3.1-pro-high")
    gather_antigravity("商品名", "ブランド", model="gemini-3.8-flash-low")
    assert "gemini-3.8-flash-low" in captured["cmd"]
    assert "gemini-3.1-pro-high" not in captured["cmd"]


def test_gather_antigravity_skips_when_command_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド") == []


def test_gather_antigravity_skips_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 120))

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド") == []


def test_gather_antigravity_skips_on_nonzero_returncode(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド") == []


def test_gather_antigravity_skips_on_empty_response(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="   \n", stderr="")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド") == []


# --------------------------------------------------------------------------
# gather_threads
# --------------------------------------------------------------------------

def test_gather_threads_builds_candidates():
    body = {"data": [
        {"id": "1", "text": "買ってよかった", "permalink": "https://threads.net/p/1"},
        {"id": "2", "text": "", "permalink": "https://threads.net/p/2"},  # 空テキストは捨てる
    ]}
    session = _FakeSession([_FakeResponse(body)])
    result = gather_threads("商品名", token="tok", session=session)
    assert len(result) == 1
    assert result[0]["source_type"] == "threads"
    assert result[0]["source_url"] == "https://threads.net/p/1"


def test_gather_threads_skips_on_permission_error():
    session = _FakeSession([_FakeResponse(status=403, text="missing threads_keyword_search")])
    result = gather_threads("商品名", token="tok", session=session)
    assert result == []


# --------------------------------------------------------------------------
# _yahoo_rating_stats
# --------------------------------------------------------------------------

def test_yahoo_rating_stats_computes_distribution():
    reviews = [{"rating": 5}, {"rating": 5}, {"rating": 3}, {"rating": "not a number"}]
    stats = _yahoo_rating_stats(reviews)
    assert stats["count"] == 3
    assert stats["average"] == pytest.approx((5 + 5 + 3) / 3, rel=1e-3)
    assert stats["distribution"] == {"5": 2, "3": 1}


def test_yahoo_rating_stats_empty():
    assert _yahoo_rating_stats([]) == {"count": 0, "average": 0.0, "distribution": {}}


def test_gather_yahoo_aggregate_reads_raw_dir(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "B0XXXXXXXX.json").write_text(json.dumps({
        "asin": "B0XXXXXXXX", "fetched_at": "2026-07-01T00:00:00Z",
        "reviews": [{"rating": 4, "title": "良い", "body": "満足しています", "posted_at": "2026-06-01"}],
    }), encoding="utf-8")
    candidates, stats = gather_yahoo_aggregate("B0XXXXXXXX", raw_dir=raw_dir)
    assert len(candidates) == 1
    assert candidates[0]["source_type"] == "yahoo_review_aggregate"
    assert candidates[0]["source_url"] == ""
    assert "満足しています" in candidates[0]["text"]
    assert stats["count"] == 1


def test_gather_yahoo_aggregate_prefers_api_review_for_rating_stats(tmp_path):
    """新形式: rating_stats は itemSearch v3 の api_review (rate/count 確定値) 優先。
    distribution はクロール本文があるときだけ補われる。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "B0APIREV01.json").write_text(json.dumps({
        "asin": "B0APIREV01", "jan_code": "4901234567890",
        "fetched_at": "2026-07-01T00:00:00Z",
        "api_review": {"rate": 4.35, "count": 12, "url": "https://x/review"},
        "reviews": [{"rating": 5, "title": "t", "body": "良かったです", "posted_at": "2026-06-01"}],
    }), encoding="utf-8")
    candidates, stats = gather_yahoo_aggregate("B0APIREV01", raw_dir=raw_dir)
    assert stats["count"] == 12          # API 由来 (本文は 1 件でも count は 12)
    assert stats["average"] == 4.35      # API 由来
    assert stats["distribution"] == {"5": 1}  # 本文由来の補完
    assert len(candidates) == 1


def test_gather_yahoo_aggregate_api_review_only_no_snippets(tmp_path):
    """reviews が空でも api_review があれば rating_stats を出す (candidates は 0 件)。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "B0APIONLY1.json").write_text(json.dumps({
        "asin": "B0APIONLY1", "jan_code": "4900000000000",
        "fetched_at": "2026-07-01T00:00:00Z",
        "api_review": {"rate": 3.9, "count": 8, "url": "https://x/review"},
        "reviews": [],
    }), encoding="utf-8")
    candidates, stats = gather_yahoo_aggregate("B0APIONLY1", raw_dir=raw_dir)
    assert candidates == []
    assert stats == {"count": 8, "average": 3.9, "distribution": {}}


# --------------------------------------------------------------------------
# extract_snippets: entailment 判定 + usable_as コード側固定割当
# --------------------------------------------------------------------------

@pytest.mark.parametrize("source_type,expected_usable_as", sorted(_USABLE_AS_MAP.items()))
def test_extract_snippets_assigns_usable_as_by_source_type(source_type, expected_usable_as):
    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "使ってみて良かったという内容の要約文です", "confidence": "high"}],
    })
    session = _FakeSession([_FakeResponse({"response": inner})])
    candidate = {"text": "本文", "source_type": source_type, "source_url": "https://example.com"}
    snippets = extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                                 session, sleeper=lambda s: None)
    assert len(snippets) == 1
    assert snippets[0]["usable_as"] == expected_usable_as
    assert snippets[0]["source_type"] == source_type


def test_extract_snippets_discards_when_not_entailed():
    inner = json.dumps({"entailed": False, "snippets": []})
    session = _FakeSession([_FakeResponse({"response": inner})])
    candidate = {"text": "無関係の文章", "source_type": "blog", "source_url": ""}
    result = extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                               session, sleeper=lambda s: None)
    assert result == []


def test_extract_snippets_empty_text_returns_empty():
    candidate = {"text": "", "source_type": "blog", "source_url": ""}
    result = extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                               _FakeSession([]), sleeper=lambda s: None)
    assert result == []


def test_extract_snippets_gives_up_after_retries_and_returns_empty():
    candidate = {"text": "本文", "source_type": "blog", "source_url": ""}
    session = _FakeSession([
        requests.ConnectionError("boom1"),
        requests.ConnectionError("boom2"),
        requests.ConnectionError("boom3"),
    ])
    result = extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                               session, sleeper=lambda s: None)
    assert result == []


# --------------------------------------------------------------------------
# resolve_product_identity
# --------------------------------------------------------------------------

def test_resolve_product_identity_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    title, product_name, brand = resolve_product_identity("B0NOTFOUND1", base=tmp_path / "per_asin")
    assert (title, product_name, brand) == ("", "", "")


# --------------------------------------------------------------------------
# mine_asin / write_experience / run: 出力スキーマと 0 件時に書かないこと
# --------------------------------------------------------------------------

def test_mine_asin_returns_none_when_no_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data" / "raw"
    data_dir.mkdir(parents=True)
    (data_dir / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0NOSOURCE1", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")
    base = data_dir / "per_asin"
    (base / "B0NOSOURCE1").mkdir(parents=True)
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("EXPERIENCE_RAW_DIR", raising=False)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)

    payload = mine_asin("B0NOSOURCE1", base=base, session=_FakeSession([]))
    assert payload is None


def test_mine_asin_builds_expected_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data" / "raw"
    data_dir.mkdir(parents=True)
    (data_dir / "amazon.json").write_text(json.dumps({"items": [
        {"asin": "B0WITHBLOG1", "title": "テスト商品 テストブランド"},
    ]}), encoding="utf-8")
    base = data_dir / "per_asin"
    asin_dir = base / "B0WITHBLOG1"
    asin_dir.mkdir(parents=True)
    (asin_dir / "third_party_sources.json").write_text(json.dumps({
        "sources": [{"url": "https://blog.example.com/review", "title": "レビュー", "snippet": "s"}],
    }), encoding="utf-8")
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("EXPERIENCE_RAW_DIR", raising=False)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)

    fetch_resp = _FakeResponse(text="<html><body>実際に使ってみたレビュー本文です</body></html>")
    inner = json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "体験談", "text": "使い心地が良いという要約テキストです", "confidence": "high"}],
    })
    gemma_resp = _FakeResponse({"response": inner})
    session = _FakeSession([fetch_resp, gemma_resp])

    payload = mine_asin("B0WITHBLOG1", base=base, session=session)
    assert payload is not None
    assert payload["asin"] == "B0WITHBLOG1"
    assert "generated_at" in payload
    assert "model" in payload
    assert payload["snippets"][0]["source_type"] == "blog"
    assert payload["snippets"][0]["usable_as"] == "quote"
    assert payload["rating_stats"] == {"yahoo": {"count": 0, "average": 0.0, "distribution": {}}}


def test_write_experience_writes_file(tmp_path):
    base = tmp_path / "per_asin"
    payload = {"asin": "B0WRITE0001", "generated_at": "now", "model": "m", "snippets": [], "rating_stats": {}}
    out_path = write_experience("B0WRITE0001", payload, base=base)
    assert out_path.exists()
    assert json.loads(out_path.read_text(encoding="utf-8"))["asin"] == "B0WRITE0001"


def test_run_does_not_write_when_mine_asin_returns_none(tmp_path, monkeypatch):
    import scripts.mine_experience as mod
    monkeypatch.setattr(mod, "mine_asin", lambda asin, **kw: None)
    base = tmp_path / "per_asin"
    summary = mod.run(["B0EMPTY0001"], base=base)
    assert summary["written"] == 0
    assert summary["skipped"] == 1
    assert not (base / "B0EMPTY0001").exists()


def test_run_writes_when_mine_asin_returns_payload(tmp_path, monkeypatch):
    import scripts.mine_experience as mod
    payload = {"asin": "B0FULL00001", "generated_at": "now", "model": "m",
               "snippets": [{"aspect": "体験談", "text": "t", "source_type": "blog",
                              "source_url": "", "usable_as": "quote", "confidence": "high"}],
               "rating_stats": {"yahoo": {"count": 0, "average": 0.0, "distribution": {}}}}
    monkeypatch.setattr(mod, "mine_asin", lambda asin, **kw: payload)
    base = tmp_path / "per_asin"
    summary = mod.run(["B0FULL00001"], base=base)
    assert summary["written"] == 1
    out_path = base / "B0FULL00001" / "experience.json"
    assert out_path.exists()
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["snippets"][0]["usable_as"] == "quote"


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch):
    import scripts.mine_experience as mod
    called = []
    monkeypatch.setattr(mod, "mine_asin", lambda asin, **kw: called.append(asin))
    base = tmp_path / "per_asin"
    summary = mod.run(["B0DRYRUN001"], base=base, dry_run=True)
    assert called == []
    assert summary["written"] == 0
    assert not base.exists() or not any(base.iterdir())
