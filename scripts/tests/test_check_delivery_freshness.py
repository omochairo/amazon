"""check_delivery_freshness.py の単体テスト (#5042 / #6205 T9)。

判定材料は build.json (`sha` / `built_at`)。sitemap の lastmod は
**2026-08-28 の 19 時間停止を検知できなかった**ため廃止した。理由は
対象モジュールの docstring 参照。

カバレッジ:
1. parse_iso: Z 終端 / オフセット / tz 無し / 壊れた値
2. check: ok / stale / 閾値の境界 / behind / unreachable / unknown
3. behind の誤報防止: HEAD が動いた直後 (= HEAD がまだ新しい) は鳴らない
4. unverified: 配信中の sha が main の履歴に無い / 優先度 / 判定不能を叫ばない
5. get_sha_on_main: compare API の status と 404 の解釈
6. title_for / render_body
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_delivery_freshness as cdf  # noqa: E402

NOW = dt.datetime(2026, 8, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
URL = "https://navi.omcha.jp/build.json"
SHA = "edba7e4c76dd314a295b4a1e86e3e5f205e6cfaa"
OTHER_SHA = "c09c9bcfaeced3a3dd28e529df0bf8ac78969d67"


def _payload(built_at: str, sha: str = SHA) -> str:
    return json.dumps({"sha": sha, "built_at": built_at, "ref": "main"})


def _fetch(body: str):
    return lambda url: body


def _raises(exc: Exception):
    def _f(url):
        raise exc
    return _f


# --- parse_iso --------------------------------------------------------------

def test_parses_z_terminated_and_normalizes_to_utc():
    assert cdf.parse_iso("2026-08-30T11:00:00Z") == dt.datetime(
        2026, 8, 30, 11, 0, tzinfo=dt.timezone.utc)


def test_non_utc_offset_is_converted():
    assert cdf.parse_iso("2026-08-30T20:00:00+09:00") == dt.datetime(
        2026, 8, 30, 11, 0, tzinfo=dt.timezone.utc)


def test_naive_value_is_treated_as_utc():
    # ここで捨てると、tz を落とす実装に変わった瞬間に誤報する。
    assert cdf.parse_iso("2026-08-30T11:00:00") == dt.datetime(
        2026, 8, 30, 11, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("value", ["", "   ", "not-a-date", None, 12345, {}])
def test_unparseable_values_yield_none(value):
    assert cdf.parse_iso(value) is None


# --- 鮮度 -------------------------------------------------------------------

def test_measured_reality_at_introduction_does_not_fire():
    # 2026-08-30 実測: build から反映まで数分。導入日に鳴らないこと。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z")))
    assert row["status"] == "ok"
    assert row["age_hours"] == 0.1


def test_stale_when_older_than_threshold():
    # 閾値は明示的に渡す。既定値 (DEFAULT_MAX_AGE_HOURS) を変えたときに
    # 境界のテストが道連れで壊れないようにするため。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T04:00:00Z")), max_age_hours=3)
    assert row["status"] == "stale"
    assert row["age_hours"] == 8.0


def test_boundary_just_inside_threshold_is_ok():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T09:00:00Z")), max_age_hours=3)
    assert row["status"] == "ok"


def test_boundary_just_outside_threshold_is_stale():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T08:59:00Z")), max_age_hours=3)
    assert row["status"] == "stale"


def test_custom_threshold_is_honoured():
    body = _payload("2026-08-30T04:00:00Z")
    assert cdf.check(URL, NOW, _fetch(body), max_age_hours=12)["status"] == "ok"
    assert cdf.check(URL, NOW, _fetch(body), max_age_hours=2)["status"] == "stale"


def test_nineteen_hour_outage_is_detected():
    # 2026-08-28 の実障害の再現。この長さで鳴らないなら網の意味がない。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-29T17:00:00Z")))
    assert row["status"] == "stale"
    assert row["age_hours"] == 19.0


# --- behind (時計は進んでいるのに中身が古い) --------------------------------

def test_behind_when_head_is_old_and_not_deployed():
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
        head={"sha": OTHER_SHA, "committed_at": "2026-08-30T01:00:00Z"})
    assert row["status"] == "behind"
    assert OTHER_SHA[:10] in row["detail"]


def test_not_behind_when_head_moved_recently():
    # HEAD が動いた直後は sha が違って当たり前。ここで鳴らすと毎回誤報になる。
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
        head={"sha": OTHER_SHA, "committed_at": "2026-08-30T11:50:00Z"})
    assert row["status"] == "ok"


def test_not_behind_when_deployed_sha_matches_head():
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
        head={"sha": SHA, "committed_at": "2026-08-30T01:00:00Z"})
    assert row["status"] == "ok"


def test_head_unavailable_still_judges_freshness():
    # HEAD を取れなくても鮮度判定は続ける (監視を丸ごと落とさない)。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T04:00:00Z")), head=None,
                    max_age_hours=3)
    assert row["status"] == "stale"
    assert row["head_sha"] is None


def test_stale_takes_precedence_over_behind():
    # 止まっているなら、まず止まっていることを言う。
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-29T17:00:00Z", sha=SHA)),
        head={"sha": OTHER_SHA, "committed_at": "2026-08-30T05:00:00Z"})
    assert row["status"] == "stale"


# --- 判定不能を ok に潰さない -----------------------------------------------

def test_fetch_failure_is_unreachable_not_ok():
    row = cdf.check(URL, NOW, _raises(cdf.FetchError("HTTP 503")))
    assert row["status"] == "unreachable"
    assert "503" in row["detail"]


def test_broken_json_is_unknown_not_ok():
    row = cdf.check(URL, NOW, _fetch("<html>not json</html>"))
    assert row["status"] == "unknown"


def test_json_array_is_unknown_not_ok():
    row = cdf.check(URL, NOW, _fetch("[]"))
    assert row["status"] == "unknown"


def test_missing_built_at_is_unknown_not_ok():
    row = cdf.check(URL, NOW, _fetch(json.dumps({"sha": SHA})))
    assert row["status"] == "unknown"
    assert row["sha"] == SHA


# --- 出力 -------------------------------------------------------------------

@pytest.mark.parametrize(
    "status", ["unverified", "stale", "behind", "unreachable", "unknown"])
def test_every_abnormal_status_has_its_own_title(status):
    title = cdf.title_for({"status": status})
    assert title.startswith("[delivery]")
    assert title != cdf.title_for({"status": "something-else"})


def test_body_carries_marker_and_numbers():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-29T17:00:00Z")))
    body = cdf.render_body(row, NOW)
    assert cdf.MARKER in body
    assert "19.0h" in body
    assert SHA in body


def test_body_of_unreachable_shows_detail():
    row = cdf.check(URL, NOW, _raises(cdf.FetchError("HTTP 503")))
    body = cdf.render_body(row, NOW)
    assert "HTTP 503" in body
    assert "unreachable" in body


def test_default_threshold_is_calibrated_against_measured_gaps():
    # 2026-08-31 実測: main への commit 間隔は直近 7 日で最大 6.2h、3h 超が 4 回。
    # 3h だと週に 4 回の誤報になるため 8h にしてある。
    # **下げるときは間隔を測り直すこと** (平均ではなく裾を見る)。
    assert cdf.DEFAULT_MAX_AGE_HOURS == 8


# --- unverified (配信中の sha が main の履歴に無い) --------------------------
#
# NAS 移管後、配信ツリーに書けるのは NAS_DEPLOY_KEY を持つ GitLab CI だけ。
# その権限が奪われたときの検知はここしかない。behind は「main の HEAD 自体が
# 閾値より古い」ときにしか立たないので、この網が無いと main が動いている限り
# 偽リリースは ok のまま通る。

def _verify(result):
    return lambda sha: result


def test_sha_not_on_main_is_unverified():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
                    verify_sha=_verify(False))
    assert row["status"] == "unverified"
    assert row["sha_verified"] is False
    assert SHA[:10] in row["detail"]


def test_sha_on_main_is_ok():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
                    verify_sha=_verify(True))
    assert row["status"] == "ok"
    assert row["sha_verified"] is True


def test_unverifiable_sha_does_not_cry_wolf():
    # GitHub API が落ちているだけで「改竄」を叫ばない。判定不能は None のまま残す。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
                    verify_sha=_verify(None))
    assert row["status"] == "ok"
    assert row["sha_verified"] is None


def test_verification_is_opt_in():
    # verify_sha を渡さなければ照合しない (既存の呼び出し元を壊さない)。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)))
    assert row["status"] == "ok"
    assert row["sha_verified"] is None


def test_unverified_takes_precedence_over_stale():
    # 古いものが配られているより、main に無いものが配られているほうが重い。
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-29T17:00:00Z", sha=SHA)),
                    verify_sha=_verify(False))
    assert row["status"] == "unverified"


def test_unverified_takes_precedence_over_behind():
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
        head={"sha": OTHER_SHA, "committed_at": "2026-08-30T01:00:00Z"},
        verify_sha=_verify(False))
    assert row["status"] == "unverified"


def test_mirror_lag_one_commit_behind_is_not_unverified():
    # 配信中の sha が HEAD と違っても、main の祖先なら正常。
    # ここを「HEAD と一致」で判定すると毎回誤報になる。
    row = cdf.check(
        URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
        head={"sha": OTHER_SHA, "committed_at": "2026-08-30T11:50:00Z"},
        verify_sha=_verify(True))
    assert row["status"] == "ok"


def test_unreachable_skips_verification_entirely():
    calls = []

    def _spy(sha):
        calls.append(sha)
        return False

    row = cdf.check(URL, NOW, _raises(cdf.FetchError("HTTP 503")), verify_sha=_spy)
    assert row["status"] == "unreachable"
    assert calls == []


# --- get_sha_on_main --------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("identical", True),   # 配信中の sha が HEAD そのもの
    ("behind", True),      # mirror 遅延。sha は main の祖先
    ("ahead", False),      # main に取り込まれていないコミット
    ("diverged", False),   # main の履歴から外れている
    ("weird", None),       # 知らない値を True にも False にも倒さない
])
def test_get_sha_on_main_maps_compare_status(monkeypatch, status, expected):
    monkeypatch.setattr(cdf, "_gh", lambda args: json.dumps({"status": status}))
    assert cdf.get_sha_on_main("o/r", SHA) is expected


def test_get_sha_on_main_treats_404_as_not_on_main():
    import subprocess

    def _boom(args):
        raise subprocess.CalledProcessError(
            1, args, output="", stderr="gh: Not Found (HTTP 404)")

    cdf_gh = cdf._gh
    try:
        cdf._gh = _boom
        assert cdf.get_sha_on_main("o/r", SHA) is False
    finally:
        cdf._gh = cdf_gh


def test_get_sha_on_main_treats_other_failures_as_undetermined():
    # レート制限や認証切れで「改竄」を叫ばない。
    import subprocess

    def _boom(args):
        raise subprocess.CalledProcessError(
            1, args, output="", stderr="gh: API rate limit exceeded (HTTP 403)")

    cdf_gh = cdf._gh
    try:
        cdf._gh = _boom
        assert cdf.get_sha_on_main("o/r", SHA) is None
    finally:
        cdf._gh = cdf_gh


def test_get_sha_on_main_ignores_empty_sha():
    assert cdf.get_sha_on_main("o/r", "") is None


# --- 出力 (unverified) ------------------------------------------------------

def test_body_of_unverified_names_the_suspicion():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z", sha=SHA)),
                    verify_sha=_verify(False))
    body = cdf.render_body(row, NOW)
    assert "unverified" in body
    assert "**いいえ**" in body
    assert "NAS_DEPLOY_KEY" in body


def test_body_shows_unverified_state_as_unknown_when_not_checked():
    row = cdf.check(URL, NOW, _fetch(_payload("2026-08-30T11:55:00Z")))
    assert "未確認" in cdf.render_body(row, NOW)
