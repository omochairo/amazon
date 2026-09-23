"""scripts/mine_experience.py unit tests (#3203 Phase 2)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import requests

from scripts import mine_experience
from scripts.mine_experience import (
    TimingTracker,
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

_COVERAGE_NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _iso_days_ago(days: float, now: datetime = _COVERAGE_NOW) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_experience_generated(base: pathlib.Path, asin: str, days_ago: float) -> None:
    d = base / asin
    d.mkdir(parents=True, exist_ok=True)
    (d / "experience.json").write_text(
        json.dumps({"generated_at": _iso_days_ago(days_ago)}), encoding="utf-8",
    )


def _write_article(articles_dir: pathlib.Path, asin: str, *, mtime_days_ago: float | None = None,
                    date_prefix: str = "2026-01-01") -> pathlib.Path:
    articles_dir.mkdir(parents=True, exist_ok=True)
    f = articles_dir / f"{date_prefix}-{asin}.json"
    f.write_text("{}", encoding="utf-8")
    if mtime_days_ago is not None:
        ts = (_COVERAGE_NOW - timedelta(days=mtime_days_ago)).timestamp()
        os.utime(f, (ts, ts))
    return f

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
        articles_dir=tmp_path / "articles_missing",
    )
    assert targets == ["B0EXPLICI1"]


def test_select_targets_respects_limit(tmp_path):
    targets = select_targets(
        limit=2, asins=["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC"],
        audit_path=tmp_path / "missing.json",
        rewrite_queue_dir=tmp_path / "missing_dir",
        articles_dir=tmp_path / "articles_missing",
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
# select_targets: 未採掘優先の並び (#4841)
# --------------------------------------------------------------------------

def _setup_queue(tmp_path: pathlib.Path, asins: list[str]) -> pathlib.Path:
    queue = tmp_path / "queue"
    queue.mkdir(exist_ok=True)
    for i, a in enumerate(asins):
        (queue / f"{i:03d}.json").write_text(json.dumps({"asin": a}), encoding="utf-8")
    return queue


def test_select_targets_skips_fresh_asin_when_limit_is_tight(tmp_path, monkeypatch):
    """generated_at が新しく、記事も更新されていない ASIN は limit 争いで負ける。"""
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB", "B0NEVER0AB"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)

    targets = select_targets(
        limit=1, audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=tmp_path / "articles_missing", now=_COVERAGE_NOW,
    )
    assert targets == ["B0NEVER0AB"]


def test_select_targets_remines_when_article_rewritten_after_generated_at(tmp_path, monkeypatch):
    """generated_at 自体は新しくても、記事が後から更新されていれば再採掘対象になる。"""
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB", "B0REWRITE1"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)
    _write_experience_generated(base, "B0REWRITE1", days_ago=5)
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "B0FRESH0AB", mtime_days_ago=10)  # experience.json より前 = リライト無し
    _write_article(articles_dir, "B0REWRITE1", mtime_days_ago=1)    # experience.json より後 = リライト有り

    targets = select_targets(
        limit=1, audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=articles_dir, now=_COVERAGE_NOW,
    )
    assert targets == ["B0REWRITE1"]


def test_select_targets_remines_when_older_than_remine_after_days(tmp_path, monkeypatch):
    """記事の更新が無くても、generated_at が remine_after_days を超えていれば再採掘対象になる。"""
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB", "B0STALE0AB"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)
    _write_experience_generated(base, "B0STALE0AB", days_ago=91)

    targets = select_targets(
        limit=1, audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=tmp_path / "articles_missing",
        remine_after_days=90, now=_COVERAGE_NOW,
    )
    assert targets == ["B0STALE0AB"]


def test_select_targets_prioritizes_unmined_pool_and_respects_limit(tmp_path, monkeypatch):
    """記事はあるが experience.json が無い ASIN (③) が④より優先され、limit を超えない。"""
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "B0FRESH0AB", mtime_days_ago=10)
    # 未採掘 (experience.json 無し) を3件用意。ASIN 降順で作るが結果は昇順で返るはず
    for asin in ["B0UNMINED3", "B0UNMINED1", "B0UNMINED2"]:
        _write_article(articles_dir, asin)

    targets = select_targets(
        limit=2, audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=articles_dir, now=_COVERAGE_NOW,
    )
    assert targets == ["B0UNMINED1", "B0UNMINED2"]


def test_select_targets_unmined_pool_is_newest_article_first(tmp_path, monkeypatch):
    """未採掘の残り (③) は公開日の新しい順に選ばれる (#6602 新着優先)。"""
    monkeypatch.chdir(tmp_path)
    articles_dir = tmp_path / "articles"
    _write_article(articles_dir, "B0OLDEST0A", date_prefix="2026-05-18")
    _write_article(articles_dir, "B0NEWEST0A", date_prefix="2026-09-16")
    _write_article(articles_dir, "B0MIDDLE0A", date_prefix="2026-07-01")

    targets = select_targets(
        limit=2, audit_path=tmp_path / "missing.json",
        rewrite_queue_dir=tmp_path / "queue_missing",
        base=tmp_path / "per_asin", articles_dir=articles_dir, now=_COVERAGE_NOW,
    )
    assert targets == ["B0NEWEST0A", "B0MIDDLE0A"]


def test_select_targets_unmined_pool_same_date_falls_back_to_asin_order(tmp_path, monkeypatch):
    """同じ公開日の中は ASIN 昇順。日ごとの流入が limit を超えても並びが揺れない。"""
    monkeypatch.chdir(tmp_path)
    articles_dir = tmp_path / "articles"
    for asin in ["B0SAMEDAY3", "B0SAMEDAY1", "B0SAMEDAY2"]:
        _write_article(articles_dir, asin, date_prefix="2026-09-16")
    _write_article(articles_dir, "B0OLDER001", date_prefix="2026-09-15")

    targets = select_targets(
        limit=0, audit_path=tmp_path / "missing.json",
        rewrite_queue_dir=tmp_path / "queue_missing",
        base=tmp_path / "per_asin", articles_dir=articles_dir, now=_COVERAGE_NOW,
    )
    assert targets == ["B0SAMEDAY1", "B0SAMEDAY2", "B0SAMEDAY3", "B0OLDER001"]


def test_select_targets_explicit_asins_bypass_freshness_rules(tmp_path, monkeypatch):
    """--asins は fresh でも limit 争いでも常に先頭で通る。"""
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB", "B0NEVER0AB"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)

    targets = select_targets(
        limit=1, asins=["B0FRESH0AB"],
        audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=tmp_path / "articles_missing", now=_COVERAGE_NOW,
    )
    assert targets == ["B0FRESH0AB"]


def test_select_targets_is_deterministic_across_repeated_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    queue = _setup_queue(tmp_path, ["B0FRESH0AB", "B0STALE0AB", "B0NEVER0AB"])
    base = tmp_path / "per_asin"
    _write_experience_generated(base, "B0FRESH0AB", days_ago=5)
    _write_experience_generated(base, "B0STALE0AB", days_ago=91)
    articles_dir = tmp_path / "articles"
    for asin in ["B0UNMINED2", "B0UNMINED1"]:
        _write_article(articles_dir, asin)

    kwargs = dict(
        limit=0, audit_path=tmp_path / "missing.json", rewrite_queue_dir=queue,
        base=base, articles_dir=articles_dir, now=_COVERAGE_NOW,
    )
    first = select_targets(**kwargs)
    second = select_targets(**kwargs)
    assert first == second
    assert first == [
        "B0NEVER0AB", "B0STALE0AB", "B0UNMINED1", "B0UNMINED2", "B0FRESH0AB",
    ]


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


def test_gather_antigravity_skips_when_every_attempt_is_empty(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="   \n", stderr="")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None) == []
    # 初回 + _MAX_EXTRA_RETRIES 回まで粘ってから諦める
    assert len(calls) == mine_experience._MAX_EXTRA_RETRIES + 1


def test_gather_antigravity_retries_empty_response_then_succeeds(monkeypatch):
    """agy は exit 0 / status SUCCESS のまま空文字を返すことがある (#6578 実測)。

    ここを skip で握ると **レーンは緑のまま体験談だけが入って来ない**。
    静かな欠落なので、空応答だけはリトライする。
    """
    outs = ["", "  \n", "口コミ要約テキスト\n"]
    slept = []

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout=outs.pop(0))

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    result = gather_antigravity("商品名", "ブランド", sleeper=slept.append)
    assert len(result) == 1
    assert result[0]["text"] == "口コミ要約テキスト"
    assert slept == [mine_experience._RETRY_SLEEP_SECONDS] * 2


def test_gather_antigravity_does_not_retry_timeout(monkeypatch):
    """timeout のリトライは 1 回 120s を積み増すだけなので割に合わない。"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 120))

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None) == []
    assert len(calls) == 1


def test_gather_antigravity_does_not_retry_nonzero_exit(monkeypatch):
    """非ゼロ終了は認証・PATH 等リトライで直らない類が主。"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None) == []
    assert len(calls) == 1


# --------------------------------------------------------------------------
# AgyCircuitBreaker: agy が連続で何も返さない run で時間を払い続けない (#6602)
# --------------------------------------------------------------------------

def _always_empty(calls, stderr=""):
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0, stdout="", stderr=stderr)
    return fake_run


def test_breaker_trips_after_threshold_and_stops_spawning_agy(monkeypatch, capsys):
    """2026-09-11〜13 の再現: 全商品で空応答 → 1 商品 3 回ずつ払い続けて timeout。

    閾値に達したら、以降の商品では subprocess を 1 回も起こさない。
    """
    calls = []
    monkeypatch.setattr("scripts.mine_experience.subprocess.run", _always_empty(calls))
    breaker = mine_experience.AgyCircuitBreaker(threshold=3)
    per_product = mine_experience._MAX_EXTRA_RETRIES + 1

    for _ in range(3):
        assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None, breaker=breaker) == []
    assert breaker.tripped
    assert len(calls) == 3 * per_product

    for _ in range(15):
        assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None, breaker=breaker) == []
    assert len(calls) == 3 * per_product  # 増えていない
    assert breaker.summary() == {"ok": 0, "failed": 3, "skipped_by_breaker": 15, "tripped": True}
    # 黙って緑にしない: annotation は開いたときに 1 回だけ
    assert capsys.readouterr().out.count("::warning title=agy circuit breaker::") == 1


def test_breaker_does_not_trip_on_scattered_failures(monkeypatch):
    """正常な日にも空応答は散発する (直近 7 run で最大 2 連続)。それで止めない。"""
    outcomes = iter(["", "", "", "ok", "", "", "", "", "", "", "ok", "", "", ""])

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout=next(outcomes))

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    breaker = mine_experience.AgyCircuitBreaker(threshold=3)
    # 商品ごと: 失敗 / 成功 / 失敗 / 失敗 / 成功 / 失敗 (失敗 = 3 回とも空)
    results = [
        gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None, breaker=breaker)
        for _ in range(6)
    ]
    assert [bool(r) for r in results] == [False, True, False, False, True, False]
    assert not breaker.tripped
    assert breaker.summary()["ok"] == 2
    assert breaker.summary()["failed"] == 4


def test_breaker_counts_timeout_but_not_fast_failures(monkeypatch):
    """timeout は時間を払うので数える。非ゼロ終了・PATH に無いは即座に返るので数えない。"""
    breaker = mine_experience.AgyCircuitBreaker(threshold=2)

    def nonzero(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=1, stderr="boom")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", nonzero)
    for _ in range(5):
        gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None, breaker=breaker)
    assert not breaker.tripped
    assert breaker.failed == 0

    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", timeout)
    for _ in range(2):
        gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None, breaker=breaker)
    assert breaker.tripped


def test_empty_response_logs_agy_stderr(monkeypatch, caplog):
    """exit 0 の空応答でも agy は理由を stderr に書く。run ログから原因を辿れるようにする。"""
    calls = []
    reason = 'jetski: no output produced — a tool required the "command" permission'
    monkeypatch.setattr("scripts.mine_experience.subprocess.run", _always_empty(calls, stderr=reason))
    with caplog.at_level("WARNING", logger=mine_experience.logger.name):
        gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None)
    assert any("command" in r.getMessage() and "空応答" in r.getMessage() for r in caplog.records)


def test_gather_antigravity_without_breaker_is_unchanged(monkeypatch):
    """bench (bench_agy_model / bench_snippet_yield) は breaker を渡さない。挙動を変えない。"""
    calls = []
    monkeypatch.setattr("scripts.mine_experience.subprocess.run", _always_empty(calls))
    for _ in range(5):
        gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None)
    assert len(calls) == 5 * (mine_experience._MAX_EXTRA_RETRIES + 1)


def test_run_shares_one_breaker_across_asins_and_reports_it(tmp_path, monkeypatch):
    import scripts.mine_experience as mod
    seen = []

    def fake_mine_asin(asin, **kw):
        seen.append(kw["agy_breaker"])
        return None

    monkeypatch.setattr(mod, "mine_asin", fake_mine_asin)
    summary = mod.run(["B0AAAAAAAA1", "B0AAAAAAAA2"], base=tmp_path / "per_asin")
    assert seen[0] is seen[1]
    assert summary["agy"] == {"ok": 0, "failed": 0, "skipped_by_breaker": 0, "tripped": False}


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


# --------------------------------------------------------------------------
# 出典 URL の収集 (#6588)
# --------------------------------------------------------------------------

class _UrlResp:
    def __init__(self, status, url):
        self.status_code = status
        self.url = url


class _UrlSession:
    """URL -> (status, final_url) の対応表で GET を模す。"""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=None, headers=None):
        self.calls.append(url)
        entry = self.table[url]
        if isinstance(entry, Exception):
            raise entry
        return _UrlResp(*entry)


def test_build_antigravity_prompt_asks_for_both_caveats_and_urls():
    """どちらが欠けても #6588 の実測前提が崩れる。

    出典 URL だけ足すと注意点が押し出され (balance 0.56 -> 0.11)、
    注意点だけ足しても出所は辿れないまま。
    """
    p = mine_experience.build_antigravity_prompt("ケルチェッティ", "ボーネルンド")
    assert "注意点" in p
    assert "出典" in p
    assert "作文" in p
    # 「原文のまま抜粋」は書かない。read_url が headless で auto-deny され空応答になる
    assert "抜粋" not in p


def test_extract_urls_strips_trailing_punctuation():
    text = "* 好評です。出典: https://item.rakuten.co.jp/a/1/\n* [店](https://ec.example.jp/g/2/)、他。"
    assert mine_experience.extract_urls(text) == [
        "https://item.rakuten.co.jp/a/1/",
        "https://ec.example.jp/g/2/",
    ]


def test_strip_urls_removes_url_and_its_label():
    text = "* 連動が好評です。 出典: https://item.rakuten.co.jp/a/1/\n* 注意点もあります。"
    assert mine_experience.strip_urls(text) == "* 連動が好評です。\n* 注意点もあります。"


def test_is_self_domain_covers_subdomains_only():
    assert mine_experience.is_self_domain("https://navi.omcha.jp/products/x/") is True
    assert mine_experience.is_self_domain("https://omcha.jp/a/") is True
    assert mine_experience.is_self_domain("https://home.omcha.jp/a/") is True
    # suffix 一致で他人のドメインを巻き込まない
    assert mine_experience.is_self_domain("https://notomcha.jp/a/") is False
    assert mine_experience.is_self_domain("https://omcha.jp.evil.com/a/") is False


def test_resolve_source_urls_drops_self_dead_and_search_pages(monkeypatch):
    """probe (#6588) で実際に返ってきた 4 種をそのまま並べる。"""
    table = {
        # 生きている実ページ → 残す
        "https://a.example.jp/1": (200, "https://a.example.jp/1"),
        # 自社記事 → 循環になるので落とす
        "https://navi.omcha.jp/products/b00000dmd2/": (
            200, "https://navi.omcha.jp/products/b00000dmd2/"),
        # 作文された URL → 404 で落とす
        "https://item.rakuten.co.jp/babybus/5014/": (
            404, "https://item.rakuten.co.jp/babybus/5014/"),
        # 検索結果ページ → 体験談が載っていないので落とす (#5490 案B)
        "https://search.rakuten.co.jp/search/mall/x/": (
            200, "https://search.rakuten.co.jp/search/mall/x/"),
    }
    text = "\n".join(f"* 行 出典: {u}" for u in table)
    got = mine_experience.resolve_source_urls(text, session=_UrlSession(table))
    assert got == ["https://a.example.jp/1"]


def test_resolve_source_urls_follows_grounding_redirect(monkeypatch):
    """Gemini は実 URL でなく不透明なリダイレクト URL を返す。解決先を保存する。"""
    src = (f"https://{mine_experience.GROUNDING_REDIRECT_HOST}"
           f"{mine_experience.GROUNDING_REDIRECT_PATH}AUZIYQ-abc")
    final = "https://product.rakuten.co.jp/product/-/147c/"
    got = mine_experience.resolve_source_urls(
        f"* 行 出典: {src}", session=_UrlSession({src: (200, final)}))
    # 保存するのはリダイレクト URL ではなく解決先。前者は後から辿れる保証が無い
    assert got == [final]


def test_resolve_source_urls_survives_network_failure():
    """ネットワークが死んでも素材そのものは残す (URL だけ捨てる)。"""
    src = "https://a.example.jp/1"
    got = mine_experience.resolve_source_urls(
        f"* 行 出典: {src}", session=_UrlSession({src: requests.ConnectionError("boom")}))
    assert got == []


def test_resolve_source_urls_caps_and_dedupes():
    table = {f"https://a.example.jp/{i}": (200, "https://a.example.jp/same") for i in range(9)}
    text = "\n".join(f"* 行 出典: {u}" for u in table)
    got = mine_experience.resolve_source_urls(text, session=_UrlSession(table), max_urls=3)
    assert got == ["https://a.example.jp/same"]  # 解決先が同じなら 1 本に畳む


def test_gather_antigravity_attaches_source_urls_and_strips_them_from_text(monkeypatch):
    """本文は gemma に渡すので URL を除く。出所は source_urls に持つ。"""
    live = "https://a.example.jp/1"
    out = f"* 連動が好評です。 出典: {live}\n* 組み立てが難しいという指摘もあります。"

    monkeypatch.setattr(
        "scripts.mine_experience.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(returncode=0, stdout=out))
    result = gather_antigravity(
        "商品名", "ブランド", session=_UrlSession({live: (200, live)}))

    assert len(result) == 1
    assert result[0]["source_urls"] == [live]
    assert "http" not in result[0]["text"]
    assert "連動が好評です。" in result[0]["text"]
    # 合成要約なので行ごとの帰属は名乗らない (偽の精度を持たせない)
    assert result[0]["source_url"] == ""


def test_extract_snippets_propagates_source_urls(monkeypatch):
    """出所は snippet まで届かないと監査に使えない。"""
    payload = {"response": json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "durability", "text": "丈夫だという声がある", "confidence": "high"}],
    })}
    session = _FakeSession([_FakeResponse(json_body=payload)])
    candidate = {
        "text": "丈夫だという声があります。",
        "source_type": "antigravity",
        "source_url": "",
        "source_urls": ["https://a.example.jp/1", "https://b.example.jp/2"],
    }
    out = extract_snippets(candidate, "商品名", "ブランド", "http://x", "m", session)
    assert len(out) == 1
    assert out[0]["source_urls"] == ["https://a.example.jp/1", "https://b.example.jp/2"]
    assert out[0]["usable_as"] == "paraphrase"


def test_extract_snippets_defaults_source_urls_for_other_lanes(monkeypatch):
    """出典 URL を返さないレーンでもキーは在る (読み手が分岐しなくて済む)。"""
    payload = {"response": json.dumps({
        "entailed": True,
        "snippets": [{"aspect": "durability", "text": "丈夫", "confidence": "high"}],
    })}
    session = _FakeSession([_FakeResponse(json_body=payload)])
    out = extract_snippets(
        {"text": "丈夫です。", "source_type": "blog", "source_url": "https://blog.example.jp/1"},
        "商品名", "ブランド", "http://x", "m", session)
    assert out[0]["source_urls"] == []
    assert out[0]["source_url"] == "https://blog.example.jp/1"


def test_gather_antigravity_falls_back_when_dbus_missing(monkeypatch):
    """手元検証用の Windows には dbus-run-session が無い。

    本番 (K8 Linux) は dbus 経由が正なので、そちらを先に試す順序は崩さない。
    """
    tried = []

    def fake_run(cmd, **kwargs):
        tried.append(cmd[0])
        if cmd[0] == "dbus-run-session":
            raise FileNotFoundError("dbus-run-session")
        return _FakeCompletedProcess(returncode=0, stdout="口コミ要約テキスト")

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    result = gather_antigravity("商品名", "ブランド")
    assert len(result) == 1
    assert tried == ["dbus-run-session", "agy"]


def test_gather_antigravity_skips_when_neither_command_exists(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("scripts.mine_experience.subprocess.run", fake_run)
    assert gather_antigravity("商品名", "ブランド", sleeper=lambda _s: None) == []


# --------------------------------------------------------------------------
# Yahoo レビューは 1 candidate にまとめる (#6602 P1)
# --------------------------------------------------------------------------
#
# 以前は 1 レビュー = 1 candidate = gemma 1 コールだった。実測で per_review は
# 4 ASIN 中 3 ASIN で不満を 1 件も拾えず (Yahoo は 97% が 4 星以上)、
# batched は 4 ASIN すべてで拾った。速さ (111.3s -> 29.0s/ASIN) は結果であって
# 選定理由ではない。ここで守るのは「まとめること」と「全レビューが入ること」。

def _write_reviews(tmp_path, reviews, asin="B0BATCH001"):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / f"{asin}.json").write_text(json.dumps({
        "asin": asin, "fetched_at": "2026-09-06T00:00:00Z",
        "api_review": {"rate": 4.6, "count": len(reviews), "url": "https://x/r"},
        "reviews": reviews,
    }, ensure_ascii=False), encoding="utf-8")
    return raw_dir


def test_yahoo_reviews_become_a_single_candidate(tmp_path):
    """10 レビューで gemma を 10 回叩かない。"""
    reviews = [{"rating": 5, "title": f"題{i}", "body": f"本文{i}"} for i in range(10)]
    candidates, _ = gather_yahoo_aggregate("B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    assert len(candidates) == 1


def test_every_review_survives_into_the_candidate(tmp_path):
    """1 件でも落ちると、落ちたのが少数派の不満だったときに効いてしまう。"""
    reviews = [{"rating": 5, "title": "", "body": f"レビュー本文{i}"} for i in range(10)]
    candidates, _ = gather_yahoo_aggregate("B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    text = candidates[0]["text"]
    for i in range(10):
        assert f"レビュー本文{i}" in text


def test_long_reviews_do_not_crowd_out_later_ones(tmp_path):
    """長い賞賛レビューが枠を食って、後ろの不満が切り捨てられないこと。

    これを落とすと「まとめた意味」が消える — 賞賛の長文だけ残る。
    """
    reviews = [{"rating": 5, "title": "", "body": "満足です。" * 900} for _ in range(9)]
    reviews.append({"rating": 2, "title": "", "body": "ここが不満だった"})
    candidates, _ = gather_yahoo_aggregate("B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    assert "ここが不満だった" in candidates[0]["text"]


def test_candidate_respects_max_candidate_text_len(tmp_path):
    reviews = [{"rating": 5, "title": "", "body": "あ" * 2000} for _ in range(10)]
    candidates, _ = gather_yahoo_aggregate("B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    from scripts.mine_experience import MAX_CANDIDATE_TEXT_LEN
    assert len(candidates[0]["text"]) <= MAX_CANDIDATE_TEXT_LEN


def test_max_reviews_still_caps_the_input(tmp_path):
    reviews = [{"rating": 5, "title": "", "body": f"本文{i}"} for i in range(30)]
    candidates, _ = gather_yahoo_aggregate(
        "B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews), max_reviews=10)
    text = candidates[0]["text"]
    assert "本文9" in text
    assert "本文10" not in text


def test_empty_bodies_produce_no_candidate(tmp_path):
    reviews = [{"rating": 5, "title": "", "body": "  "} for _ in range(3)]
    candidates, stats = gather_yahoo_aggregate(
        "B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    assert candidates == []
    # rating_stats は api_review 由来なので残る
    assert stats["count"] == 3


def test_candidate_keeps_aggregate_framing(tmp_path):
    """usable_as=paraphrase の前提。行ごとの帰属を名乗らない。"""
    reviews = [{"rating": 5, "title": "題", "body": "本文"}]
    candidates, _ = gather_yahoo_aggregate("B0BATCH001", raw_dir=_write_reviews(tmp_path, reviews))
    assert candidates[0]["source_type"] == "yahoo_review_aggregate"
    assert candidates[0]["source_url"] == ""


# --------------------------------------------------------------------------
# select_mining_targets: 鮮度を見た対象選定 + K8 volume 上の ledger (#6602)
# --------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

_NOW = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool(tmp_path, asins):
    queue = tmp_path / "queue"
    queue.mkdir(exist_ok=True)
    for i, a in enumerate(asins):
        (queue / f"{i:03d}.json").write_text(json.dumps({"asin": a}), encoding="utf-8")
    # articles_dir は既定で実リポジトリの data/articles を見に行く (#4841) ので、
    # これらのテストが実データを拾わないよう存在しないディレクトリに逸らす。
    return {
        "audit_path": tmp_path / "missing.json", "rewrite_queue_dir": queue,
        "articles_dir": tmp_path / "articles_missing",
    }


def _exp(base, asin, days_ago):
    d = base / asin
    d.mkdir(parents=True, exist_ok=True)
    (d / "experience.json").write_text(json.dumps({"generated_at": _iso(days_ago)}), encoding="utf-8")


def test_mining_selection_skips_fresh_and_fills_limit_from_the_rest(tmp_path):
    """09-07〜13 の再現: 先頭 3 件が毎日掘り直され、後ろが永遠に回ってこない。"""
    base = tmp_path / "per_asin"
    asins = ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3", "B0AAAAAAA4", "B0AAAAAAA5"]
    for a in asins[:3]:
        _exp(base, a, days_ago=1)
    targets, report = mine_experience.select_mining_targets(
        limit=3, base=base, ledger={}, now=_NOW, **_pool(tmp_path, asins))
    assert targets == ["B0AAAAAAA4", "B0AAAAAAA5"]
    assert report["counts"] == {"fresh": 3, "never": 2}


def test_mining_selection_priority_never_then_degraded_then_oldest(tmp_path):
    base = tmp_path / "per_asin"
    asins = ["B0STALENEW", "B0STALEOLD", "B0DEGRADED", "B0NEVER001", "B0NOYIELD1"]
    stale_new, stale_old, degraded, never, noyield = asins
    _exp(base, stale_new, days_ago=35)
    _exp(base, stale_old, days_ago=60)
    _exp(base, degraded, days_ago=10)
    ledger = {
        degraded: {"last_attempt": _iso(10), "written": True, "agy": "failed"},
        noyield: {"last_attempt": _iso(40), "written": False, "agy": "ok"},
    }
    targets, report = mine_experience.select_mining_targets(
        limit=0, base=base, ledger=ledger, now=_NOW, **_pool(tmp_path, asins))
    assert targets == [never, degraded, noyield, stale_old, stale_new]
    assert [s["reason"] for s in report["selected"]] == [
        "never", "agy_degraded", "no_yield_retry", "stale", "stale"]


def test_mining_selection_does_not_retry_recent_no_yield(tmp_path):
    """0 件の ASIN はファイルが書かれない。ledger が無いと毎日先頭に居座る。"""
    asin = "B0NOYIELD1"
    ledger = {asin: {"last_attempt": _iso(3), "written": False, "agy": "ok"}}
    targets, report = mine_experience.select_mining_targets(
        limit=0, base=tmp_path / "per_asin", ledger=ledger, now=_NOW, **_pool(tmp_path, [asin]))
    assert targets == []
    assert report["counts"] == {"no_yield_recent": 1}


def test_mining_selection_agy_degraded_waits_a_week_and_needs_matching_attempt(tmp_path):
    base = tmp_path / "per_asin"
    recent, old_ledger = "B0DEGRADE1", "B0DEGRADE2"
    _exp(base, recent, days_ago=3)
    _exp(base, old_ledger, days_ago=10)
    ledger = {
        recent: {"last_attempt": _iso(3), "written": True, "agy": "breaker"},
        # ledger の最終試行が experience.json より古い = その回の記録ではない
        old_ledger: {"last_attempt": _iso(20), "written": True, "agy": "failed"},
    }
    targets, report = mine_experience.select_mining_targets(
        limit=0, base=base, ledger=ledger, now=_NOW, **_pool(tmp_path, [recent, old_ledger]))
    assert targets == []
    assert report["counts"] == {"fresh": 2}


def test_mining_selection_explicit_asins_bypass_freshness_and_come_first(tmp_path):
    base = tmp_path / "per_asin"
    _exp(base, "B0EXPLICI1", days_ago=1)
    targets, report = mine_experience.select_mining_targets(
        limit=2, asins=["B0EXPLICI1"], base=base, ledger={}, now=_NOW,
        **_pool(tmp_path, ["B0AAAAAAA1", "B0AAAAAAA2"]))
    assert targets == ["B0EXPLICI1", "B0AAAAAAA1"]
    assert report["selected"][0]["reason"] == "explicit"


def test_ledger_missing_or_broken_starts_empty(tmp_path):
    assert mine_experience.load_ledger(tmp_path / "none.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert mine_experience.load_ledger(broken) == {}


def test_ledger_round_trip(tmp_path):
    path = tmp_path / "vol" / "mining_ledger.json"
    mine_experience.save_ledger(path, {"B0AAAAAAA1": {"written": True}})
    assert mine_experience.load_ledger(path) == {"B0AAAAAAA1": {"written": True}}
    assert not path.with_suffix(".json.tmp").exists()


def test_default_ledger_lives_next_to_raw_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("EXPERIENCE_LEDGER", raising=False)
    monkeypatch.setenv("EXPERIENCE_RAW_DIR", str(tmp_path))
    assert mine_experience.default_ledger_path() == tmp_path / "mining_ledger.json"


def test_run_records_every_attempt_in_ledger_and_job_summary(tmp_path, monkeypatch):
    """0 件の回も ledger に残す。Job Summary には ASIN ごとに 1 行ずつ追記する。"""
    import scripts.mine_experience as mod
    payload = {"asin": "x", "generated_at": "now", "model": "m", "rating_stats": {},
               "snippets": [{"aspect": "不満", "text": "t"}]}

    def fake_mine_asin(asin, **kw):
        kw["agy_breaker"].record_ok()
        return payload if asin == "B0WRITTEN1" else None

    monkeypatch.setattr(mod, "mine_asin", fake_mine_asin)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    ledger_path = tmp_path / "vol" / "mining_ledger.json"

    mod.run(["B0WRITTEN1", "B0NOYIELD1"], base=tmp_path / "per_asin", ledger_path=ledger_path,
            ledger={}, reasons={"B0WRITTEN1": "never", "B0NOYIELD1": "stale"})

    saved = mod.load_ledger(ledger_path)
    assert saved["B0WRITTEN1"]["written"] is True
    assert saved["B0WRITTEN1"]["snippets"] == 1
    assert saved["B0WRITTEN1"]["agy"] == "ok"
    assert saved["B0NOYIELD1"]["written"] is False
    text = summary_file.read_text(encoding="utf-8")
    assert "| B0WRITTEN1 | 未処理 | 書けた | 1 | ok |" in text
    assert "| B0NOYIELD1 | 期限切れ | 0 件 | 0 | ok |" in text
    assert "**完了**" in text


def test_run_saves_ledger_after_each_asin(tmp_path, monkeypatch):
    """step timeout で打ち切られても、そこまでの試行が残る。"""
    import scripts.mine_experience as mod
    ledger_path = tmp_path / "mining_ledger.json"
    seen = []

    def fake_mine_asin(asin, **kw):
        seen.append(set(mod.load_ledger(ledger_path)))
        return None

    monkeypatch.setattr(mod, "mine_asin", fake_mine_asin)
    mod.run(["B0AAAAAAA1", "B0AAAAAAA2"], base=tmp_path, ledger_path=ledger_path, ledger={})
    assert seen == [set(), {"B0AAAAAAA1"}]


def test_report_selection_writes_coverage_to_job_summary(tmp_path, monkeypatch):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    mine_experience.report_selection({
        "candidates": 80, "ledger_entries": 5,
        "counts": {"fresh": 19, "never": 61},
        "selected": [{"asin": "B0AAAAAAA1", "reason": "never"}],
    })
    text = summary_file.read_text(encoding="utf-8")
    assert "候補 **80** 件" in text and "**19** 件 (24%)" in text
    assert "| 未処理 | 61 |" in text
    assert "今回掘る: **1** 件 (未処理 1)" in text


def test_report_selection_shows_backlog_eta_when_present(tmp_path, monkeypatch):
    """#6602: 未処理を回り切るのに何 run かかるかを Job Summary で見えるようにする。"""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    mine_experience.report_selection({
        "candidates": 2437, "ledger_entries": 125,
        "counts": {"fresh": 11, "never": 2404, "no_yield_recent": 21, "stale": 1},
        "selected": [{"asin": "B0AAAAAAA1", "reason": "never"}] * 20,
        "limit": 20, "backlog_remaining": 2406,
    })
    text = summary_file.read_text(encoding="utf-8")
    assert "残り **2406** 件" in text
    assert "最短 **121 run**" in text  # ceil(2406 / 20)


def test_report_selection_omits_eta_when_backlog_field_absent(tmp_path, monkeypatch):
    """limit/backlog_remaining を渡さない旧呼び出しでも壊れない。"""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    mine_experience.report_selection({
        "candidates": 80, "ledger_entries": 5,
        "counts": {"fresh": 19, "never": 61},
        "selected": [{"asin": "B0AAAAAAA1", "reason": "never"}],
    })
    text = summary_file.read_text(encoding="utf-8")
    assert "最短" not in text


def test_select_mining_targets_reports_limit_and_backlog_remaining(tmp_path):
    base = tmp_path / "per_asin"
    asins = [f"B0AAAAAA{i:02d}" for i in range(5)]
    targets, report = mine_experience.select_mining_targets(
        limit=2, base=base, ledger={}, now=_NOW, **_pool(tmp_path, asins))
    assert len(targets) == 2
    assert report["limit"] == 2
    assert report["backlog_remaining"] == 3  # 5 件が未処理・うち2件だけ今回選ばれた


def test_backlog_remaining_excludes_explicit_asins(tmp_path):
    """--asins は eligible の外から来る。残数を負にしたり過小に出したりしない。"""
    base = tmp_path / "per_asin"
    asins = [f"B0AAAAAA{i:02d}" for i in range(5)]
    pool = _pool(tmp_path, asins)
    _, report = mine_experience.select_mining_targets(
        limit=20, asins=["B0ZZZZZZ99"], base=base, ledger={}, now=_NOW, **pool)
    assert report["backlog_remaining"] == 0  # 5 件すべて採用済み。-1 にしない

    _, report = mine_experience.select_mining_targets(
        limit=3, asins=["B0ZZZZZZ99"], base=base, ledger={}, now=_NOW, **pool)
    assert report["backlog_remaining"] == 3  # 3 枠のうち 1 枠は explicit、掘れたのは 2 件


def test_crawl_uses_the_same_selection_as_mining():
    """原文を取る ASIN と掘る ASIN がずれると、掘る側で Yahoo 候補が 0 件になる。"""
    src = pathlib.Path("scripts/crawl_yahoo_reviews.py").read_text(encoding="utf-8")
    assert "select_mining_targets(" in src
    assert "select_targets(limit" not in src


# --- run 単位の「応答しないホスト」skip (#6602) ---------------------------------

def _fake_http_send(timeout_hosts: set[str], calls: list[str]):
    def send(self, request, *args, **kwargs):
        calls.append(request.url)
        host = requests.utils.urlparse(request.url).hostname
        if host in timeout_hosts:
            raise requests.ReadTimeout("read timed out", request=request)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        resp._content = b"<html><body>ok</body></html>"
        resp.request = request
        return resp
    return send


def test_dead_host_is_skipped_for_rest_of_run_after_one_timeout(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        _fake_http_send({"www.biccamera.com"}, calls))
    session, adapter = mine_experience.make_session("http://localhost:11434")

    for _ in range(3):
        with pytest.raises(requests.RequestException):
            session.get("https://www.biccamera.com/bc/item/1/", timeout=20)
    assert session.get("https://example.com/", timeout=20).status_code == 200

    # timeout を払うのは最初の 1 回だけ。2 回目以降は送らない
    assert calls == ["https://www.biccamera.com/bc/item/1/", "https://example.com/"]
    assert adapter.summary() == {"hosts": ["www.biccamera.com"], "skipped_requests": 2}


def test_dead_host_skip_is_a_request_exception_so_callers_just_drop_the_url(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        _fake_http_send({"www.yodobashi.com"}, calls))
    session, _ = mine_experience.make_session()
    text = "口コミ https://www.yodobashi.com/a https://www.yodobashi.com/b https://example.com/c"

    assert mine_experience.resolve_source_urls(text, session=session) == ["https://example.com/c"]
    assert calls == ["https://www.yodobashi.com/a", "https://example.com/c"]


def test_ollama_host_is_never_marked_dead(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        _fake_http_send({"k8-ollama"}, calls))
    session, adapter = mine_experience.make_session("http://k8-ollama:11434")

    for _ in range(2):
        with pytest.raises(requests.ReadTimeout):
            session.post("http://k8-ollama:11434/api/generate", json={}, timeout=180)
    # 1 回の遅い応答で gemma を止めると、その run の抽出が全部死ぬ
    assert len(calls) == 2
    assert adapter.summary() == {"hosts": [], "skipped_requests": 0}


def test_non_timeout_errors_do_not_mark_host_dead(monkeypatch):
    def send(self, request, *args, **kwargs):
        raise requests.ConnectionError("Name or service not known", request=request)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    session, adapter = mine_experience.make_session()

    for _ in range(2):
        with pytest.raises(requests.ConnectionError):
            session.get("https://hoobby.net/x", timeout=20)
    assert adapter.summary() == {"hosts": [], "skipped_requests": 0}


def test_run_reports_dead_hosts_in_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(mine_experience, "mine_asin", lambda *a, **k: None)
    summary = run(["B000000001"], base=tmp_path)
    assert summary["dead_hosts"] == {"hosts": [], "skipped_requests": 0}


def test_dead_host_applies_to_redirect_target_of_grounding_url(monkeypatch):
    """09-14 の timeout 18 件のうち 11 件は grounding redirect を辿った先だった。"""
    calls: list[str] = []
    inner = _fake_http_send({"www.biccamera.com"}, calls)

    def send(self, request, *args, **kwargs):
        if "vertexaisearch" in request.url:
            calls.append(request.url)
            resp = requests.Response()
            resp.status_code = 302
            resp.headers["Location"] = "https://www.biccamera.com/bc/item/9/"
            resp.url = request.url
            resp._content = b""
            resp.request = request
            return resp
        return inner(self, request, *args, **kwargs)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    session, adapter = mine_experience.make_session()
    g = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    text = f"{g}AAA {g}BBB"

    assert mine_experience.resolve_source_urls(text, session=session) == []
    assert calls == [f"{g}AAA", "https://www.biccamera.com/bc/item/9/", f"{g}BBB"]
    assert adapter.summary() == {"hosts": ["www.biccamera.com"], "skipped_requests": 1}


# --------------------------------------------------------------------------
# TimingTracker (#6602 P2: gemma / agy / http / other の内訳計測、観測のみ)
# --------------------------------------------------------------------------

def test_timing_tracker_summary_defaults_to_zero_without_duration_fields():
    """response フィールドはあっても prompt_eval_duration 等が無ければ 0 のまま
    (旧バージョン等で欠けていても壊れない)。"""
    t = TimingTracker()
    t.record_gemma_response({"response": "{}"})
    s = t.summary()
    assert s["gemma_calls"] == 1
    assert s["gemma_prompt_eval_seconds"] == 0.0
    assert s["gemma_eval_seconds"] == 0.0
    assert s["gemma_prompt_eval_count"] == 0
    assert s["gemma_eval_count"] == 0


def test_timing_tracker_record_gemma_response_ignores_non_dict_payload():
    t = TimingTracker()
    t.record_gemma_response("not a dict")
    assert t.gemma_calls == 0


def test_timing_tracker_add_accumulates_per_bucket():
    t = TimingTracker()
    t.add("gemma", 1.0)
    t.add("gemma", 0.5)
    t.add("agy", 2.0)
    assert t.summary()["seconds"] == {"agy": 2.0, "gemma": 1.5, "http": 0.0, "other": 0.0}


def test_extract_snippets_records_gemma_response_stats_when_timing_given():
    """prompt_eval_duration/eval_duration (ns) と count を timing に積算する。"""
    inner = json.dumps({"entailed": True, "snippets": []})
    resp_json = {
        "response": inner,
        "prompt_eval_duration": 1_500_000_000,
        "eval_duration": 500_000_000,
        "prompt_eval_count": 300,
        "eval_count": 20,
    }
    session = _FakeSession([_FakeResponse(resp_json)])
    candidate = {"text": "本文", "source_type": "blog", "source_url": ""}
    timing = TimingTracker()
    extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                      session, sleeper=lambda s: None, timing=timing)
    s = timing.summary()
    assert s["gemma_calls"] == 1
    assert s["gemma_prompt_eval_seconds"] == pytest.approx(1.5)
    assert s["gemma_eval_seconds"] == pytest.approx(0.5)
    assert s["gemma_prompt_eval_count"] == 300
    assert s["gemma_eval_count"] == 20
    assert s["seconds"]["gemma"] >= 0.0


def test_extract_snippets_without_timing_is_unchanged():
    """timing 未指定 (既定 None) でも従来どおり動く。"""
    inner = json.dumps({"entailed": True, "snippets": []})
    session = _FakeSession([_FakeResponse({"response": inner})])
    candidate = {"text": "本文", "source_type": "blog", "source_url": ""}
    result = extract_snippets(candidate, "商品名", "ブランド", "http://ollama", "gemma4",
                               session, sleeper=lambda s: None)
    assert result == []


def test_gather_antigravity_records_agy_and_http_timing(monkeypatch):
    """agy subprocess 実行は agy バケット、出典 URL 解決は http バケットに分かれる。"""
    live = "https://a.example.jp/1"
    out = f"* 連動が好評です。 出典: {live}"
    monkeypatch.setattr(
        "scripts.mine_experience.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(returncode=0, stdout=out))
    timing = TimingTracker()
    result = gather_antigravity(
        "商品名", "ブランド", session=_UrlSession({live: (200, live)}), timing=timing)
    assert len(result) == 1
    s = timing.summary()["seconds"]
    assert s["agy"] >= 0.0
    assert s["http"] >= 0.0
    assert s["gemma"] == 0.0


def test_run_includes_timing_breakdown_in_summary_and_job_summary(tmp_path, monkeypatch):
    """run() は timing を集計し、summary['timing'] と Job Summary の両方に出す。"""
    import time as time_mod
    import scripts.mine_experience as mod

    def fake_mine_asin(asin, **kw):
        timing = kw["timing"]
        timing.add("gemma", 0.01)
        timing.record_gemma_response({
            "prompt_eval_duration": 2_000_000_000,
            "eval_duration": 1_000_000_000,
            "prompt_eval_count": 500,
            "eval_count": 50,
        })
        time_mod.sleep(0.001)  # summary['timing']['seconds']['other'] を 0 超にする
        return None

    monkeypatch.setattr(mod, "mine_asin", fake_mine_asin)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    summary = mod.run(["B0TIMING01"], base=tmp_path / "per_asin", ledger={})

    t = summary["timing"]
    assert t["gemma_calls"] == 1
    assert t["gemma_prompt_eval_seconds"] == pytest.approx(2.0)
    assert t["gemma_eval_seconds"] == pytest.approx(1.0)
    assert t["gemma_prompt_eval_count"] == 500
    assert t["gemma_eval_count"] == 50
    assert t["seconds"]["gemma"] >= 0.01
    assert t["seconds"]["other"] >= 0.0

    text = summary_file.read_text(encoding="utf-8")
    assert "所要時間の内訳" in text
    assert "gemma 呼び出し 1 回" in text


def test_run_timing_is_all_zero_in_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(mine_experience, "mine_asin", lambda *a, **k: None)
    summary = run(["B0DRYRUN01"], base=tmp_path, dry_run=True)
    assert summary["timing"]["seconds"] == {"agy": 0.0, "gemma": 0.0, "http": 0.0, "other": 0.0}
    assert summary["timing"]["gemma_calls"] == 0
