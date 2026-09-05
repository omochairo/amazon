"""Tests for scripts/jules_quota_gate.py (session 114 真因修正)。

実 session 作成数 (rolling-24h) ベースで 1 run の作成予算を返すゲートの純粋ロジック検証。
API は偽の urlopen に差し替え、count_created_24h / remaining_budget の純粋ロジックと
fetch_sessions のページング・retry・欠測時の挙動 (2026-09-05) を検証する。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import jules_quota_gate as _gate  # noqa: E402
from jules_quota_gate import count_created_24h, remaining_budget  # noqa: E402


def _iso(hours_ago: float) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_count_created_24h_window() -> None:
    sessions = [
        {"createTime": _iso(1)},    # in window
        {"createTime": _iso(23)},   # in window
        {"createTime": _iso(25)},   # aged out
        {"createTime": _iso(100)},  # aged out
        {"createTime": None},       # missing → 無視
        {},                         # createTime 欠落 → 無視
    ]
    assert count_created_24h(sessions) == 2


def test_remaining_budget_plenty() -> None:
    # 作成 10 / cap 80 → 残り 70 だが per-run max 12 でクランプ
    assert remaining_budget(10, cap=80, per_run_max=12) == 12


def test_remaining_budget_near_cap() -> None:
    # 作成 77 / cap 80 → 残り 3 (per-run max 未満なのでそのまま)
    assert remaining_budget(77, cap=80, per_run_max=12) == 3


def test_remaining_budget_exhausted() -> None:
    # 作成 80 / cap 80 → 0
    assert remaining_budget(80, cap=80, per_run_max=12) == 0


def test_remaining_budget_over_cap_clamps_to_zero() -> None:
    # 旧バグ状況 (実 100 作成) でも負数を返さず 0
    assert remaining_budget(100, cap=80, per_run_max=12) == 0


# --- fetch_sessions のページング / retry (2026-09-05) -------------------------
#
# LIST /sessions が 30s timeout・retry 無しで恒常的に落ち、ゲートが fallback 1 を返して
# 生成本数が 1/6 に落ちたまま緑で終わっていた。ここで守るのは 3 点:
#   1. 一過性エラーは retry で回復する (以前は 1 発勝負だった)
#   2. retry を使い切ったら partial を返さず送出する (過小カウント → 予算過大 = 旧 overshoot)
#   3. pageSize は既定 50 で送る (1 リクエストの仕事量を減らす狙い)


class _FakeUrlopen:
    """urllib.request.urlopen の差し替え。呼び出しごとに script の要素を消費する。

    要素が Exception なら送出、dict ならその JSON を返すレスポンスとして振る舞う。
    """

    def __init__(self, script):
        self.script = list(script)
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(json.dumps(item).encode('utf-8'))


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, script):
    fake = _FakeUrlopen(script)
    monkeypatch.setattr(_gate.urllib.request, 'urlopen', fake)
    return fake


def test_fetch_sessions_paginates(monkeypatch) -> None:
    fake = _patch_urlopen(monkeypatch, [
        {'sessions': [{'createTime': _iso(1)}], 'nextPageToken': 't2'},
        {'sessions': [{'createTime': _iso(2)}]},
    ])
    out = _gate.fetch_sessions('k', sleep=lambda _s: None)
    assert len(out) == 2
    assert 'pageToken=t2' in fake.urls[1]


def test_fetch_sessions_sends_default_page_size(monkeypatch) -> None:
    fake = _patch_urlopen(monkeypatch, [{'sessions': []}])
    _gate.fetch_sessions('k', sleep=lambda _s: None)
    assert 'pageSize=50' in fake.urls[0]


def test_fetch_sessions_retries_transient_error(monkeypatch) -> None:
    waits = []
    fake = _patch_urlopen(monkeypatch, [
        TimeoutError('read timed out'),
        TimeoutError('read timed out'),
        {'sessions': [{'createTime': _iso(1)}]},
    ])
    out = _gate.fetch_sessions('k', backoff=1, sleep=waits.append)
    assert len(out) == 1
    # 3 回目で成功 → 待機は 2 回 (1s, 2s の指数バックオフ)
    assert waits == [1, 2]
    assert len(fake.urls) == 3


def test_fetch_sessions_raises_after_attempts_exhausted(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, [TimeoutError('x'), TimeoutError('x'), TimeoutError('x')])
    with pytest.raises(TimeoutError):
        _gate.fetch_sessions('k', sleep=lambda _s: None)


def test_fetch_sessions_does_not_return_partial_pages(monkeypatch) -> None:
    """2 ページ目が落ちたら 1 ページ目だけを返さない。

    partial を返すと rolling-24h の作成数が過小になり、ゲートが予算を過大に出す。
    session 114 で踏んだ overshoot と同じ壊れ方なので、欠測は fallback 側に倒す。
    """
    _patch_urlopen(monkeypatch, [
        {'sessions': [{'createTime': _iso(1)}], 'nextPageToken': 't2'},
        TimeoutError('x'), TimeoutError('x'), TimeoutError('x'),
    ])
    with pytest.raises(TimeoutError):
        _gate.fetch_sessions('k', sleep=lambda _s: None)


def test_warn_step_summary_appends(tmp_path, monkeypatch) -> None:
    f = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(f))
    _gate.warn_step_summary('API 取得失敗')
    assert 'jules_quota_gate fallback' in f.read_text(encoding='utf-8')


def test_warn_step_summary_noop_without_env(monkeypatch) -> None:
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    _gate.warn_step_summary('x')  # 例外を投げないこと (ローカル実行でゲートを落とさない)
