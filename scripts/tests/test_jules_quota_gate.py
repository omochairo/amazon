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


# --- 全体の持ち時間 (deadline) -------------------------------------------------
#
# ページ単位の retry しか無いと 30 ページ × attempts × timeout ぶん待ちうる。
# 実際 #6501 直後の 12-rewrite-idle-fill が該当ステップで 10 分以上返らなくなった。
# fallback に倒れるまでを wall-clock で切る。


class _Clock:
    """monotonic の差し替え。呼ばれるたびに進む偽の時計。"""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.now
        self.now += self.step
        return v

    def advance(self, sec: float) -> None:
        self.now += sec


def test_fetch_sessions_raises_deadline_across_pages(monkeypatch) -> None:
    """ページを跨いで持ち時間を使い切ったら partial を返さず打ち切る。"""
    clock = _Clock()

    def _urlopen(req, timeout=None):
        clock.advance(100)  # 1 ページに 100s 掛かる想定
        return _FakeResp(json.dumps(
            {'sessions': [{'createTime': _iso(1)}], 'nextPageToken': 'next'}
        ).encode('utf-8'))

    monkeypatch.setattr(_gate.urllib.request, 'urlopen', _urlopen)
    with pytest.raises(_gate.DeadlineExceeded):
        _gate.fetch_sessions('k', deadline=180, sleep=lambda _s: None, monotonic=clock)


def test_fetch_sessions_does_not_sleep_past_deadline(monkeypatch) -> None:
    """バックオフで持ち時間が尽きるなら、待たずに打ち切る。"""
    clock = _Clock()
    slept = []

    def _urlopen(req, timeout=None):
        clock.advance(50)
        raise TimeoutError('read timed out')

    monkeypatch.setattr(_gate.urllib.request, 'urlopen', _urlopen)
    with pytest.raises(_gate.DeadlineExceeded):
        _gate.fetch_sessions(
            'k', deadline=55, backoff=30, sleep=slept.append, monotonic=clock,
        )
    assert slept == []  # 30s 待つと 55s を超えるので待たない


def test_fetch_page_clamps_timeout_to_remaining(monkeypatch) -> None:
    """socket timeout は残り持ち時間を超えない。"""
    seen = []

    def _urlopen(req, timeout=None):
        seen.append(timeout)
        return _FakeResp(json.dumps({'sessions': []}).encode('utf-8'))

    monkeypatch.setattr(_gate.urllib.request, 'urlopen', _urlopen)
    _gate.fetch_sessions(
        'k', timeout=60, deadline=10, sleep=lambda _s: None, monotonic=_Clock(),
    )
    assert seen == [10]


def test_fetch_sessions_without_deadline_is_unbounded(monkeypatch) -> None:
    """deadline=None なら従来どおり時間で打ち切らない (呼び出し側が選べる)。"""
    clock = _Clock()

    def _urlopen(req, timeout=None):
        clock.advance(10_000)
        return _FakeResp(json.dumps({'sessions': [{'createTime': _iso(1)}]}).encode('utf-8'))

    monkeypatch.setattr(_gate.urllib.request, 'urlopen', _urlopen)
    out = _gate.fetch_sessions('k', deadline=None, sleep=lambda _s: None, monotonic=clock)
    assert len(out) == 1


def test_fetch_sessions_keeps_session_coverage(monkeypatch) -> None:
    """pageSize を下げても走査する session 総数を減らさない。

    旧実装は 100 件 × 30 ページ = 3,000。pageSize だけ 50 に下げて max_pages を
    据え置くと被覆が半減し、API が新しい順に返していなかった場合に rolling-24h の
    作成数を取りこぼす (= 予算過大 = session 114 の overshoot)。並び順は未確認なので
    被覆は減らさない。
    """
    pages = []

    def _urlopen(req, timeout=None):
        pages.append(req.full_url)
        return _FakeResp(json.dumps(
            {'sessions': [], 'nextPageToken': 'more'}
        ).encode('utf-8'))

    monkeypatch.setattr(_gate.urllib.request, 'urlopen', _urlopen)
    _gate.fetch_sessions('k', deadline=None, sleep=lambda _s: None)
    assert len(pages) * _gate.DEFAULT_PAGE_SIZE >= 3000


# --- 早期打ち切り (2026-09-05) ------------------------------------------------
#
# 全ページ走査は実測 14 分。欲しいのは直近 24h の作成数だけなので、**並びが降順で
# あることを確認できたときだけ** 24h 境界で打ち切る。降順でなければ全部読む。


def _page(hours, token=None):
    body = {'sessions': [{'createTime': _iso(h)} for h in hours]}
    if token:
        body['nextPageToken'] = token
    return body


def test_early_stop_when_desc_and_past_24h(monkeypatch) -> None:
    """降順が確認でき、ページ末尾が 24h より古ければ次のページを読まない。"""
    fake = _patch_urlopen(monkeypatch, [
        _page([1, 2, 3], token='p2'),
        _page([20, 25], token='p3'),      # 末尾 25h → ここで打ち切り
        _page([100], token='p4'),         # 読まれないはず
    ])
    n = _gate.count_created_24h_early_stop('k', sleep=lambda _s: None)
    assert n == 4              # 1,2,3,20 の 4 件
    assert len(fake.urls) == 2  # 3 ページ目は読んでいない


def test_no_early_stop_when_not_sorted_desc(monkeypatch) -> None:
    """降順でなければ打ち切らない (取りこぼすと予算過大になるため)。"""
    fake = _patch_urlopen(monkeypatch, [
        _page([30, 1], token='p2'),   # 昇順混じり = 降順ではない
        _page([2]),                   # 最後まで読む
    ])
    n = _gate.count_created_24h_early_stop('k', sleep=lambda _s: None)
    assert n == 2               # 1h と 2h
    assert len(fake.urls) == 2


def test_early_stop_reads_all_when_all_recent(monkeypatch) -> None:
    """24h 以内しか無ければ最後まで読む。"""
    fake = _patch_urlopen(monkeypatch, [
        _page([1, 2], token='p2'),
        _page([3, 4]),
    ])
    assert _gate.count_created_24h_early_stop('k', sleep=lambda _s: None) == 4
    assert len(fake.urls) == 2
