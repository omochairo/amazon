"""failover_dns.py の単体テスト (#6205 B-1)。

この機能の失敗はどれも **本番 DNS を巻き込む** ので、テストの重心は
「倒すべきときに倒す」より **「倒してはいけないときに倒さない」** に置く。

カバレッジ:
1. classify_probe: cf-ray の有無が弁別子であること
2. decide: 倒す / 倒さない の全分岐
3. 倒さない側の 5 つの防壁 (ambiguous / blocked / throttled / disabled / already)
4. side_of: CNAME の向き先判定
5. probe_origin: 正常時 1 回・異常時のみ追試
6. render_body / ログ: **トンネルのホスト名が public な出力に漏れないこと**
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

import failover_dns as fd  # noqa: E402

NOW = dt.datetime(2026, 8, 31, 12, 0, 0, tzinfo=dt.timezone.utc)
SHA = "e33ef80616e02ba4a89804dba338945345d48861"
TUNNEL = "8f3c1d0e-1111-2222-3333-444455556666.cfargotunnel.com"
STANDBY = fd.DEFAULT_STANDBY_CNAME


def probe_ok() -> dict:
    return {"url": "u", "status": 200, "cf_ray": "abc-NRT", "error": None,
            "body": json.dumps({"sha": SHA, "built_at": "2026-08-31T11:50:00Z"})}


def probe_origin_down(status: int = 521) -> dict:
    return {"url": "u", "status": status, "cf_ray": "abc-NRT", "error": None,
            "body": "<html>Error 1033</html>"}


def probe_unreachable() -> dict:
    return {"url": "u", "status": None, "cf_ray": None, "body": None,
            "error": "ConnectionError: boom"}


def standby_ok(built_at: str = "2026-08-31T11:50:00Z") -> dict:
    return {"status": 200, "cf_ray": None, "error": None,
            "body": json.dumps({"sha": SHA, "built_at": built_at})}


def record(content: str = TUNNEL, modified_on: str = "2026-08-20T00:00:00Z") -> dict:
    return {"id": "rec1", "name": "navi.omcha.jp", "type": "CNAME",
            "content": content, "proxied": True, "ttl": 1,
            "modified_on": modified_on}


def call(probes, standby=None, rec=None, enabled=True, **kw):
    return fd.decide(probes, standby if standby is not None else standby_ok(),
                     rec if rec is not None else record(), NOW,
                     enabled=enabled, **kw)


# --- 1. classify_probe -----------------------------------------------------

def test_200_with_valid_json_is_healthy():
    assert fd.classify_probe(probe_ok()) == "healthy"


def test_200_with_unreadable_body_is_not_origin_down():
    """中身が壊れていても「オリジン障害」ではない。倒す理由にしない。"""
    p = probe_ok()
    p["body"] = "<html>not json</html>"
    assert fd.classify_probe(p) == "ambiguous"


@pytest.mark.parametrize("status", sorted(fd.CF_ORIGIN_ERROR_CODES))
def test_cf_origin_error_codes_are_origin_down(status):
    assert fd.classify_probe(probe_origin_down(status)) == "origin_down"


def test_5xx_without_cf_ray_is_ambiguous():
    """cf-ray が無い 5xx は CF より手前の問題。CNAME を書き換えても直らない。"""
    p = probe_origin_down(522)
    p["cf_ray"] = None
    assert fd.classify_probe(p) == "ambiguous"


def test_connection_error_is_ambiguous():
    assert fd.classify_probe(probe_unreachable()) == "ambiguous"


def test_404_with_cf_ray_is_ambiguous():
    """オリジンが応答した上での 4xx はオリジン障害ではない。"""
    p = probe_origin_down(404)
    assert fd.classify_probe(p) == "ambiguous"


# --- 2. 倒す ---------------------------------------------------------------

def test_all_probes_origin_down_triggers_failover():
    row = call([probe_origin_down()] * 3)
    assert row["action"] == "failover"
    assert row["target"] == STANDBY


def test_healthy_probe_anywhere_means_no_action():
    """1 回でも応答したら倒さない (連続失敗の要求を裏から言っている)。"""
    row = call([probe_origin_down(), probe_origin_down(), probe_ok()])
    assert row["action"] == "none"


# --- 3. 倒さない側の防壁 ---------------------------------------------------

def test_mixed_ambiguous_does_not_failover():
    row = call([probe_origin_down(), probe_unreachable(), probe_origin_down()])
    assert row["action"] == "ambiguous"


def test_all_unreachable_does_not_failover():
    """**全滅していても cf-ray が無ければ倒さない。** 最も踏みやすい誤作動。"""
    row = call([probe_unreachable()] * 3)
    assert row["action"] == "ambiguous"


def test_standby_stale_blocks_failover():
    row = call([probe_origin_down()] * 3,
               standby=standby_ok(built_at="2026-08-29T00:00:00Z"))  # 60h
    assert row["action"] == "blocked"
    assert "古い" in row["detail"]


def test_quiet_period_standby_still_allows_failover():
    """main への commit 間隔は実測で最大 6.2h ある。**静かなだけの待機系を
    stale 扱いして倒せなくしない** (しきい値を分布の中央に置かない)。"""
    row = call([probe_origin_down()] * 3,
               standby=standby_ok(built_at="2026-08-31T05:00:00Z"))  # 7h
    assert row["action"] == "failover"


def test_standby_unreachable_blocks_failover():
    row = call([probe_origin_down()] * 3,
               standby={"status": None, "error": "Timeout", "body": None})
    assert row["action"] == "blocked"


def test_standby_non_200_blocks_failover():
    row = call([probe_origin_down()] * 3,
               standby={"status": 503, "error": None, "body": ""})
    assert row["action"] == "blocked"


def test_cooldown_blocks_repeated_flip():
    row = call([probe_origin_down()] * 3,
               rec=record(modified_on="2026-08-31T11:00:00Z"))
    assert row["action"] == "throttled"


def test_cooldown_expired_allows_flip():
    row = call([probe_origin_down()] * 3,
               rec=record(modified_on="2026-08-31T09:00:00Z"))
    assert row["action"] == "failover"


def test_kill_switch_blocks_write():
    row = call([probe_origin_down()] * 3, enabled=False)
    assert row["action"] == "disabled"


def test_already_on_standby_is_not_flipped_again():
    row = call([probe_origin_down()] * 3, rec=record(content=STANDBY))
    assert row["action"] == "already"


def test_unknown_cname_is_never_touched():
    """向き先が想定外なら自動で書き換えない (人が何かしている最中かもしれない)。"""
    row = call([probe_origin_down()] * 3, rec=record(content="example.com"))
    assert row["action"] == "blocked"


def test_missing_record():
    row = fd.decide([probe_origin_down()], standby_ok(), None, NOW, enabled=True)
    assert row["action"] == "no_record"


def test_empty_probes_is_ambiguous():
    row = call([])
    assert row["action"] == "ambiguous"


def test_standby_not_probed_when_origin_healthy():
    """正常時は待機系の状態が何であれ none。"""
    row = call([probe_ok()], standby={"error": "未確認"})
    assert row["action"] == "none"


# --- 4. side_of -----------------------------------------------------------

@pytest.mark.parametrize("content,expected", [
    (TUNNEL, "nas"),
    (TUNNEL.upper(), "nas"),
    (TUNNEL + ".", "nas"),
    (STANDBY, "standby"),
    (STANDBY.upper(), "standby"),
    ("omocha.gitlab.io", "unknown"),
    ("", "unknown"),
])
def test_side_of(content, expected):
    assert fd.side_of(content) == expected


# --- 5. probe_origin ------------------------------------------------------

def test_probe_origin_stops_after_first_healthy():
    calls = []

    def fake(url):
        calls.append(url)
        return probe_ok()

    out = fd.probe_origin("u", attempts=3, interval=0, timeout=1,
                          sleep=lambda _s: None, probe=fake)
    assert len(out) == 1 and len(calls) == 1


def test_probe_origin_retries_on_failure_and_sleeps_between():
    slept = []

    out = fd.probe_origin("u", attempts=3, interval=20, timeout=1,
                          sleep=slept.append, probe=lambda _u: probe_origin_down())
    assert len(out) == 3
    # 追試の前だけ待つ (1 回目の前には待たない)。
    assert slept == [20, 20]


def test_probe_origin_stops_when_it_recovers_midway():
    seq = [probe_origin_down(), probe_ok(), probe_origin_down()]
    out = fd.probe_origin("u", attempts=3, interval=0, timeout=1,
                          sleep=lambda _s: None, probe=lambda _u: seq.pop(0))
    assert len(out) == 2


# --- 6. public な出力にトンネルのホスト名を出さない ------------------------
#
# このリポジトリは public で、issue 本文も Actions のログも全世界に永久に残る。
# navi.omcha.jp は proxied なので `<uuid>.cfargotunnel.com` は外から見えず、
# **ここに出すことがそのまま公開になる**。回帰させないための網。

@pytest.mark.parametrize("rec_content", [TUNNEL, STANDBY, "example.com"])
@pytest.mark.parametrize("enabled", [True, False])
def test_tunnel_hostname_never_appears_in_issue_body(rec_content, enabled):
    row = call([probe_origin_down()] * 3, rec=record(content=rec_content),
               enabled=enabled)
    body = fd.render_body(row)
    assert TUNNEL not in body
    assert "cfargotunnel.com" not in body


def test_decide_row_does_not_carry_the_raw_cname():
    """row 自体に値を持たせない (ログや将来の出力に漏れる経路を断つ)。"""
    row = call([probe_origin_down()] * 3)
    assert "current_content" not in row
    assert TUNNEL not in json.dumps(row, ensure_ascii=False, default=str)


def test_describe_side_labels_without_the_value():
    assert fd.describe_side(TUNNEL) == "本番 (cloudflared tunnel)"
    assert TUNNEL not in fd.describe_side(TUNNEL)
    assert STANDBY in fd.describe_side(STANDBY)   # 待機系は公開ホスト名なので可
    assert fd.describe_side("example.com") == "**想定外の値**"


def test_unknown_side_detail_does_not_quote_the_value():
    row = call([probe_origin_down()] * 3, rec=record(content="secret-host.example"))
    assert "secret-host.example" not in row["detail"]


def test_render_body_carries_marker_and_action():
    row = call([probe_origin_down()] * 3)
    body = fd.render_body(row)
    assert "<!-- {} -->".format(fd.MARKER) in body
    assert "`failover`" in body


def test_title_for_covers_every_action():
    """判定を増やしたらタイトルも足す。汎用文言に落ちるのを検知する。"""
    actions = ["failover", "disabled", "blocked", "throttled", "already",
               "ambiguous", "no_record"]
    for action in actions:
        assert fd.title_for({"action": action}) == fd.TITLES[action]


# --- parse_iso ------------------------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("2026-08-31T11:50:00Z", True),
    ("2026-08-31T11:50:00+00:00", True),
    ("2026-08-31T11:50:00", True),   # tz 無しは UTC とみなす
    ("not a date", False),
    ("", False),
    (None, False),
])
def test_parse_iso(value, ok):
    assert (fd.parse_iso(value) is not None) is ok


# --- get_open_issue -------------------------------------------------------
#
# 2026-09-02 の実害: 本文に「監視 53 (origin failover) との分担」と書いてあった
# #6415 が自分の issue と誤認され、自動 close された。検索は絞り込みでしかなく、
# 採用の判定は本文のマーカーで行う。

def _search_result(items):
    return json.dumps({"items": items})


def test_get_open_issue_skips_issues_without_marker(monkeypatch):
    items = [
        {"number": 6415, "body": "監視 51 / 53 (origin failover) との分担を書く"},
        {"number": 999, "body": fd.MARKER_HTML + "\n本文"},
    ]
    monkeypatch.setattr(fd, "_gh", lambda *a, **k: _search_result(items))
    got = fd.get_open_issue("owner/repo")
    assert got is not None
    assert got["number"] == 999


def test_get_open_issue_returns_none_when_only_false_positives(monkeypatch):
    items = [{"number": 6415, "body": "origin failover の話をしているだけ"}]
    monkeypatch.setattr(fd, "_gh", lambda *a, **k: _search_result(items))
    assert fd.get_open_issue("owner/repo") is None


def test_get_open_issue_returns_none_when_no_hits(monkeypatch):
    monkeypatch.setattr(fd, "_gh", lambda *a, **k: _search_result([]))
    assert fd.get_open_issue("owner/repo") is None


def test_marker_html_is_embedded_in_generated_body():
    """起票する本文が MARKER_HTML を持たなくなったら、上の裏取りが全部空振りする。"""
    body = fd.render_body({"action": "failover", "now": "2026-09-06T00:00:00Z",
                           "probes": [], "probe_verdicts": []})
    assert fd.MARKER_HTML in body
