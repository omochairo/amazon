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


def test_healthy_while_on_standby_stays_already():
    """倒したあとの次の run。navi.omcha.jp は**待機系から** 200 を返す。

    これを none (= 健全) にすると状態 issue が閉じ、待機系を向いたままだと
    誰も気づかない。戻すまで already で開いたままにする。
    """
    row = call([probe_ok()], rec=record(content=STANDBY))
    assert row["action"] == "already"
    assert row["action"] not in fd.HEALTHY_ACTIONS
    assert "--to nas" in row["detail"]


def test_healthy_on_nas_is_still_none():
    row = call([probe_ok()], rec=record(content=TUNNEL))
    assert row["action"] == "none"


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


# --------------------------------------------------------------------------
# 状態 issue の特定 (#6622)
# --------------------------------------------------------------------------
#
# 2026-09-06、このレーンは無関係な epic (#6602) を「自分の状態 issue」と誤認して
# close した。原因は `in:body "origin-failover"` の本文検索で先頭 1 件を無条件に
# 採用していたこと。GitHub 検索はトークン分割するので、**レーン名に言及した
# だけ**の issue が全部ヒットする。
#
# close より怖いのは update_issue で、あれは body を丸ごと差し替えるので
# **他人の issue が中身ごと消える**。ここは「掴まないこと」を固定する。

def _install_fake_gh(monkeypatch, *, label_items=None, search_items=None, calls=None):
    """fd._gh を差し替える。呼ばれたサブコマンドを calls に記録する。"""
    def fake_gh(args):
        if calls is not None:
            calls.append(args)
        if args[0] == "issue" and args[1] == "list":
            return json.dumps(label_items if label_items is not None else [])
        if args[0] == "api" and "search/issues" in args:
            return json.dumps({"items": search_items if search_items is not None else []})
        if args[0] == "label":
            return ""
        return ""
    monkeypatch.setattr(fd, "_gh", fake_gh)


def test_issue_merely_mentioning_the_lane_is_not_adopted(monkeypatch):
    """#6622 の再現。本文にレーン名があるだけの issue を掴まない。"""
    mention_only = {
        "number": 6602,
        "body": "寄せてはいけないもの: 51/52/53 (delivery-freshness / origin-failover)",
    }
    _install_fake_gh(monkeypatch, label_items=[], search_items=[mention_only])
    assert fd.get_open_issue("omochairo/amazon") is None


def test_issue_with_marker_comment_is_adopted(monkeypatch):
    real = {"number": 42,
            "body": fd.MARKER_COMMENT + "\n\n本文"}
    _install_fake_gh(monkeypatch, label_items=[real])
    got = fd.get_open_issue("omochairo/amazon")
    assert got is not None and got["number"] == 42


def test_label_lookup_is_tried_before_body_search(monkeypatch):
    calls = []
    real = {"number": 7, "body": fd.MARKER_COMMENT}
    _install_fake_gh(monkeypatch, label_items=[real], calls=calls)
    fd.get_open_issue("omochairo/amazon")
    # ラベルで見つかったなら検索 API は叩かない
    assert any(c[0] == "issue" and c[1] == "list" for c in calls)
    assert not any(c[0] == "api" for c in calls)


def test_falls_back_to_search_for_unlabelled_legacy_issue(monkeypatch):
    """ラベルが付く前に起票された issue も拾えること (移行用)。"""
    legacy = {"number": 99, "body": fd.MARKER_COMMENT + "\n旧 issue"}
    _install_fake_gh(monkeypatch, label_items=[], search_items=[legacy])
    got = fd.get_open_issue("omochairo/amazon")
    assert got is not None and got["number"] == 99


def test_multiple_marked_issues_refuses_instead_of_guessing(monkeypatch):
    """複数あったら黙って先頭を取らない。片方を勝手に潰す方が高くつく。"""
    a = {"number": 1, "body": fd.MARKER_COMMENT}
    b = {"number": 2, "body": fd.MARKER_COMMENT}
    _install_fake_gh(monkeypatch, label_items=[a, b])
    with pytest.raises(fd.MultipleStateIssues):
        fd.get_open_issue("omochairo/amazon")


def test_search_hits_are_filtered_not_trusted(monkeypatch):
    """検索が複数返しても、マーカーを持つ 1 件だけが採用されること。"""
    noise1 = {"number": 100, "body": "origin-failover のレーンについて"}
    real = {"number": 200, "body": fd.MARKER_COMMENT + "\n状態"}
    noise2 = {"number": 300, "body": "53-origin-failover を触った"}
    _install_fake_gh(monkeypatch, label_items=[], search_items=[noise1, real, noise2])
    got = fd.get_open_issue("omochairo/amazon")
    assert got is not None and got["number"] == 200


def test_created_issue_carries_the_state_label(monkeypatch):
    """次回からラベルで引けるように、起票時に必ず付けること。"""
    calls = []
    _install_fake_gh(monkeypatch, calls=calls)
    fd.create_issue("omochairo/amazon", "t", "b")
    create = [c for c in calls if c[0] == "issue" and c[1] == "create"][0]
    labels = create[create.index("--label") + 1]
    assert fd.STATE_LABEL in labels


# --------------------------------------------------------------------------
# main: 待機系を向いている間の状態 issue
# --------------------------------------------------------------------------

def _run_main(monkeypatch, *, rec_content, existing_body=None, nas_probes=None,
              now="2026-08-31T12:00:00+00:00", auto=False, patched=None):
    """main を DNS・HTTP・gh を差し替えて 1 回走らせ、gh の呼び出しを返す。

    ``nas_probes`` は NAS 直通の観測 (既定は到達不能 = 戻さない)。
    ``patched`` に list を渡すと DNS の書き換えを許し、書き換え先を積む。
    """
    calls = []
    label_items = ([{"number": 9704, "body": existing_body}]
                   if existing_body is not None else [])
    _install_fake_gh(monkeypatch, label_items=label_items, calls=calls)
    monkeypatch.setattr(fd, "probe_origin", lambda *a, **k: [probe_ok()])
    monkeypatch.setattr(fd, "probe_stable",
                        lambda *a, **k: list(nas_probes or [probe_unreachable()]))
    monkeypatch.setattr(fd, "get_zone_id", lambda *a, **k: "zone")
    monkeypatch.setattr(fd, "get_record", lambda *a, **k: record(content=rec_content))

    def fake_patch(token, zone_id, rec, content):
        if patched is None:
            raise AssertionError("この場面で DNS を書き換えてはいけない")
        patched.append(content)
        return {"content": content}
    monkeypatch.setattr(fd, "patch_record", fake_patch)
    monkeypatch.setenv("CF_DNS_TOKEN", "t")
    monkeypatch.setenv("FAILOVER_ENABLED", "true")
    monkeypatch.setenv("NAS_ORIGIN_CNAME", TUNNEL)
    if auto:
        monkeypatch.setenv("FAILBACK_AUTO", "true")
    else:
        monkeypatch.delenv("FAILBACK_AUTO", raising=False)
    assert fd.main(["--repo", "omochairo/amazon", "--now", now]) == 0
    return calls


def _issue_ops(calls):
    return [c[1] for c in calls if c[0] == "issue" and c[1] != "list"]


def test_main_keeps_issue_open_after_failover(monkeypatch):
    """倒した直後 (本文は failover) の次の run で、close せず already に更新する。"""
    body = fd.MARKER_COMMENT + "\n" + fd._action_line("failover")
    calls = _run_main(monkeypatch, rec_content=STANDBY, existing_body=body)
    assert _issue_ops(calls) == ["edit"]
    edit = [c for c in calls if c[:2] == ["issue", "edit"]][0]
    assert edit[edit.index("--title") + 1] == fd.title_for({"action": "already"})


def test_main_does_not_rewrite_issue_that_is_already_already(monkeypatch):
    """待機系での配信中は 30 分毎に同じ内容で書き換えない (API を叩かない)。"""
    body = fd.MARKER_COMMENT + "\n" + fd._action_line("already")
    calls = _run_main(monkeypatch, rec_content=STANDBY, existing_body=body)
    assert _issue_ops(calls) == []


def test_main_opens_issue_when_on_standby_without_one(monkeypatch):
    """手で倒した等で issue が無くても、待機系を向いていれば起票する。"""
    calls = _run_main(monkeypatch, rec_content=STANDBY)
    assert _issue_ops(calls) == ["create"]


def test_main_closes_issue_once_back_on_nas(monkeypatch):
    body = fd.MARKER_COMMENT + "\n" + fd._action_line("already")
    calls = _run_main(monkeypatch, rec_content=TUNNEL, existing_body=body)
    assert _issue_ops(calls) == ["close"]


def test_main_issue_body_never_carries_the_tunnel_hostname(monkeypatch):
    calls = _run_main(monkeypatch, rec_content=STANDBY)
    for c in calls:
        assert TUNNEL not in " ".join(c)


def test_marker_comment_is_the_full_html_comment():
    """裸の語ではなくコメント形式で照合していること (これが #6622 の本体)。"""
    assert fd.MARKER_COMMENT.startswith("<!--")
    assert fd.MARKER_COMMENT.endswith("-->")
    assert fd.MARKER in fd.MARKER_COMMENT


# --------------------------------------------------------------------------
# 戻し (待機系 → NAS): 直りかけの回線で戻さないこと (2026-11-12)
# --------------------------------------------------------------------------

def _already_row():
    return fd.decide([probe_ok()], {}, record(content=STANDBY), NOW, enabled=True)


def _since(hours_ago: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=hours_ago)


def test_failback_nas_down_stays_already_and_clears_the_mark():
    row = fd.decide_failback(_already_row(), [probe_origin_down(530)], probe_ok(),
                             record(content=STANDBY), NOW, _since(5), auto=True)
    assert row["action"] == "already"
    assert row["healthy_since"] is None


def test_failback_one_bad_probe_among_good_is_not_healthy():
    row = fd.decide_failback(_already_row(), [probe_ok(), probe_unreachable()], probe_ok(),
                             record(content=STANDBY), NOW, _since(5), auto=True)
    assert row["action"] == "already"


def test_failback_first_healthy_sighting_starts_the_clock():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             record(content=STANDBY), NOW, None, auto=True)
    assert row["action"] == "failback_pending"
    assert row["healthy_since"] == NOW


def test_failback_waits_until_stable_hours():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             record(content=STANDBY), NOW, _since(1.5), auto=True)
    assert row["action"] == "failback_pending"
    assert row["healthy_since"] == _since(1.5)


def test_failback_waits_while_sha_differs():
    other = probe_ok()
    other["body"] = json.dumps({"sha": "0" * 40, "built_at": "2026-08-31T11:00:00Z"})
    row = fd.decide_failback(_already_row(), [other] * 3, probe_ok(),
                             record(content=STANDBY), NOW, _since(3), auto=True)
    assert row["action"] == "failback_pending"
    assert row["healthy_since"] == _since(3)  # NAS は生きているので刻みは残す


def test_failback_respects_cooldown_after_failover():
    rec = record(content=STANDBY, modified_on="2026-08-31T11:00:00Z")
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             rec, NOW, _since(3), auto=True)
    assert row["action"] == "failback_pending"


def test_failback_ready_when_unarmed():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             record(content=STANDBY), NOW, _since(2.5), auto=False)
    assert row["action"] == "failback_ready"


def test_failback_auto_when_armed():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             record(content=STANDBY), NOW, _since(2.5), auto=True)
    assert row["action"] == "failback_auto"


def test_healthy_since_roundtrips_through_the_issue_body():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, probe_ok(),
                             record(content=STANDBY), NOW, None, auto=False)
    body = fd.render_body(row)
    assert fd.parse_healthy_since(body) == NOW
    assert fd.parse_healthy_since("no mark") is None


def test_probe_stable_stops_at_first_failure():
    seq = [probe_ok(), probe_unreachable(), probe_ok()]
    got = fd.probe_stable("u", 3, 0, 1, sleep=lambda s: None, probe=lambda u: seq.pop(0))
    assert len(got) == 2


def _body_with(action, since=None):
    reason = fd.pending_reason_marker("stabilizing") if action == "failback_pending" else ""
    return "\n".join([fd.MARKER_COMMENT, fd.healthy_since_marker(since), reason,
                      fd._action_line(action)])


def test_main_records_first_healthy_sighting(monkeypatch):
    calls = _run_main(monkeypatch, rec_content=STANDBY, existing_body=_body_with("already"),
                      nas_probes=[probe_ok()] * 3)
    edit = [c for c in calls if c[:2] == ["issue", "edit"]][0]
    body = edit[edit.index("--body") + 1]
    assert fd.parse_healthy_since(body) == NOW
    assert fd._action_line("failback_pending") in body


def test_main_does_not_rewrite_while_pending_with_same_mark(monkeypatch):
    calls = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_pending", _since(1)),
                      nas_probes=[probe_ok()] * 3)
    assert _issue_ops(calls) == []


def test_main_resets_the_mark_when_nas_drops_again(monkeypatch):
    calls = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_pending", _since(1)),
                      nas_probes=[probe_origin_down(530)])
    edit = [c for c in calls if c[:2] == ["issue", "edit"]][0]
    body = edit[edit.index("--body") + 1]
    assert fd.parse_healthy_since(body) is None
    assert fd._action_line("already") in body


def test_main_comments_once_when_ready(monkeypatch):
    calls = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_pending", _since(3)),
                      nas_probes=[probe_ok()] * 3)
    assert _issue_ops(calls) == ["edit", "comment"]
    again = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_ready", _since(3)),
                      nas_probes=[probe_ok()] * 3)
    assert _issue_ops(again) == []


def test_main_auto_failback_patches_and_closes(monkeypatch):
    patched = []
    calls = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_pending", _since(3)),
                      nas_probes=[probe_ok()] * 3, auto=True, patched=patched)
    assert patched == [TUNNEL]
    assert _issue_ops(calls) == ["close"]
    for c in calls:
        assert TUNNEL not in " ".join(c)


def test_main_auto_failback_without_target_only_notifies(monkeypatch):
    monkeypatch.setattr(fd, "resolve_failback_target", lambda a: ("", "戻し先が分からない"))
    calls = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=_body_with("failback_pending", _since(3)),
                      nas_probes=[probe_ok()] * 3, auto=True)
    assert _issue_ops(calls) == ["edit", "comment"]


# --- agy レビューで足した分 (2026-11-12) ----------------------------------------

def test_failback_when_standby_is_also_down_does_not_wait():
    """全断 (待機系も 5xx) で NAS が応えるなら、安定も sha も待たずに戻す。"""
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, None,
                             record(content=STANDBY, modified_on="2026-08-31T11:50:00Z"),
                             NOW, None, auto=False, emergency_ok=True)
    assert row["action"] == "failback_auto"


def test_failback_when_standby_down_but_unarmed_only_notifies():
    row = fd.decide_failback(_already_row(), [probe_ok()] * 3, None,
                             record(content=STANDBY), NOW, None, auto=False, emergency_ok=False)
    assert row["action"] == "failback_ready"


def test_main_checks_nas_even_when_standby_is_down(monkeypatch):
    patched = []
    calls = []
    _install_fake_gh(monkeypatch, label_items=[], calls=calls)
    monkeypatch.setattr(fd, "probe_origin", lambda *a, **k: [probe_origin_down(530)] * 3)
    monkeypatch.setattr(fd, "probe_stable", lambda *a, **k: [probe_ok()] * 3)
    monkeypatch.setattr(fd, "http_probe", lambda *a, **k: probe_origin_down(530))
    monkeypatch.setattr(fd, "get_zone_id", lambda *a, **k: "zone")
    monkeypatch.setattr(fd, "get_record", lambda *a, **k: record(content=STANDBY))
    monkeypatch.setattr(fd, "patch_record",
                        lambda t, z, r, c: patched.append(c) or {"content": c})
    monkeypatch.setenv("CF_DNS_TOKEN", "t")
    monkeypatch.setenv("FAILOVER_ENABLED", "true")
    monkeypatch.setenv("NAS_ORIGIN_CNAME", TUNNEL)
    monkeypatch.delenv("FAILBACK_AUTO", raising=False)
    assert fd.main(["--repo", "omochairo/amazon", "--now", NOW.isoformat()]) == 0
    assert patched == [TUNNEL]


def test_pending_reason_change_rewrites_the_body(monkeypatch):
    """安定待ち → sha 待ちに変わったら、同じ failback_pending でも書き換える。"""
    other = probe_ok()
    other["body"] = json.dumps({"sha": "0" * 40, "built_at": "2026-08-31T11:00:00Z"})
    body = "\n".join([fd.MARKER_COMMENT, fd.healthy_since_marker(_since(3)),
                      fd.pending_reason_marker("stabilizing"),
                      fd._action_line("failback_pending")])
    calls = _run_main(monkeypatch, rec_content=STANDBY, existing_body=body,
                      nas_probes=[other] * 3)
    edit = [c for c in calls if c[:2] == ["issue", "edit"]][0]
    assert fd.parse_pending_reason(edit[edit.index("--body") + 1]) == "sha_mismatch"
    again = _run_main(monkeypatch, rec_content=STANDBY,
                      existing_body=edit[edit.index("--body") + 1], nas_probes=[other] * 3)
    assert _issue_ops(again) == []
