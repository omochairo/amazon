"""source_status (候補供給 P1b: 商品ごとの出典の状態) のテスト。"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_status as ss  # noqa: E402

TODAY = dt.date(2026, 11, 23)


def test_record_states_and_recheck_dates():
    st = {}
    e = ss.record(st, "B0A", sites=2, need=2, checked_urls=["u1"], today=TODAY, new_product=False)
    assert e["state"] == "writable" and "recheck_after" not in e and e["checks"] == 1
    e = ss.record(st, "B0A", sites=1, need=2, checked_urls=["u1", "u2"], today=TODAY, new_product=False)
    assert e["state"] == "insufficient" and e["recheck_after"] == "2027-02-21" and e["checks"] == 2
    assert e["checked_urls"] == ["u1", "u2"]


def test_new_product_schedule_from_first_seen():
    st = {}
    # 初観測 11-15 → 14 日目 11-29 が次の見直し日
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=TODAY, new_product=True, first="2026-11-15")
    assert e == {**e, "state": "waiting_new", "recheck_after": "2026-11-29", "first_seen": "2026-11-15"}
    # 30 日目を過ぎたら 60 日目
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=dt.date(2026, 12, 20), new_product=True)
    assert e["recheck_after"] == "2027-01-14" and e["first_seen"] == "2026-11-15"  # 前の行の初観測を引き継ぐ
    # 60 日を過ぎたら insufficient
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=dt.date(2027, 1, 20), new_product=True)
    assert e["state"] == "insufficient"


def test_new_product_without_history_uses_first_check_date():
    st = {}
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=TODAY, new_product=True)
    assert e["first_seen"] == "2026-11-23" and e["first_seen_approx"] and e["recheck_after"] == "2026-12-07"
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=dt.date(2026, 12, 7), new_product=True)
    assert e["first_seen"] == "2026-11-23" and e["recheck_after"] == "2026-12-23"  # 起点は動かない
    e = ss.record(st, "B0N", sites=0, need=1, checked_urls=[], today=dt.date(2027, 1, 22), new_product=True)
    assert e["state"] == "insufficient"
    # 通常品は初観測を作らない
    assert "first_seen" not in ss.record({}, "B0O", sites=0, need=2, checked_urls=[], today=TODAY,
                                         new_product=False)


def test_article_states():
    st = {}
    e = ss.record(st, "B0R", sites=0, need=2, checked_urls=["u"], today=TODAY, new_product=False, has_article=True)
    assert e["state"] == "article_short" and e["recheck_after"] == "2026-12-23"
    e = ss.record(st, "B0R", sites=2, need=2, checked_urls=["u"], today=TODAY, new_product=False, has_article=True)
    assert e["state"] == "writable_article" and "recheck_after" not in e


def test_due():
    assert ss.due(None, [], TODAY) == (True, "unchecked")
    assert ss.due({"state": "writable"}, [], TODAY)[0]
    assert ss.due({"state": "writable_article"}, [], TODAY) == (True, "rewrite_queued")
    e = {"state": "insufficient", "checked_urls": ["a", "b"], "recheck_after": "2027-01-01"}
    assert ss.due(e, ["a"], TODAY) == (False, "insufficient")  # リンク切れで減っただけ
    assert ss.due(e, ["a", "c"], TODAY) == (True, "new_urls")
    assert ss.due(e, ["a"], dt.date(2027, 1, 1)) == (True, "recheck_due")
    assert ss.due({"state": "article_short", "checked_urls": [], "recheck_after": "2027-01-01"}, [], TODAY)[0] is False


def test_merge_keeps_newer_per_asin():
    base = {"B0A": {"checked_at": "2026-11-20", "state": "insufficient"},
            "B0B": {"checked_at": "2026-11-23", "state": "writable"}}
    ours = {"B0A": {"checked_at": "2026-11-23", "state": "writable"},
            "B0B": {"checked_at": "2026-11-21", "state": "insufficient"},
            "B0C": {"checked_at": "2026-11-23", "state": "insufficient"}}
    m = ss.merge(base, ours)
    assert m["B0A"]["state"] == "writable" and m["B0B"]["state"] == "writable" and "B0C" in m


def test_first_seen_and_save_load(tmp_path):
    h = tmp_path / "history"
    h.mkdir()
    (h / "B0A.jsonl").write_text('{"ts": "2026-07-13T07:27:38+00:00", "price": 1}\n'
                                 '{"ts": "2026-08-01T00:00:00+00:00"}\n', encoding="utf-8")
    assert ss.first_seen("B0A", str(h)) == "2026-07-13"
    assert ss.first_seen("B0Z", str(h)) is None
    p = tmp_path / "s" / "source_status.json"
    ss.save(p, {"B0B": {"state": "writable"}, "B0A": {"state": "insufficient"}})
    assert list(json.loads(p.read_text(encoding="utf-8"))) == ["B0A", "B0B"]
    assert ss.load(p)["B0A"]["state"] == "insufficient"
    assert ss.load(tmp_path / "missing.json") == {}
