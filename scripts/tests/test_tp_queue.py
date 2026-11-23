"""tp_queue (候補供給 P2: 第三者ソースの収集待ち列) のテスト。"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tp_queue as tq  # noqa: E402

TODAY = dt.date(2026, 11, 23)


def _w(root: Path, rel: str, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_window_asins_reads_all_pools(tmp_path):
    _w(tmp_path, "data/raw/amazon.json", {"items": [{"asin": "B0AAAAAAA1"}, {"asin": "bad"}]})
    _w(tmp_path, "data/raw/ranking_pool.json", {"asins": ["B0BBBBBBB2"]})
    _w(tmp_path, "data/raw/first_party_pool.json", {"asins": ["B0CCCCCCC3", "x"]})
    assert tq.window_asins(str(tmp_path)) == {"B0AAAAAAA1", "B0BBBBBBB2", "B0CCCCCCC3"}
    assert tq.window_asins(str(tmp_path / "none")) == set()


def test_add_only_newly_seen_and_unfetched(tmp_path):
    per = tmp_path / "per_asin"
    _w(per, "B0NEW00002/third_party_sources.json", {})  # 収集済み → 足さない
    q = per / "_tp_queue.json"
    added = tq.add({"B0OLD00001"}, {"B0OLD00001", "B0NEW00001", "B0NEW00002"}, TODAY, q, per)
    assert added == ["B0NEW00001"]
    assert tq.load(q) == {"B0NEW00001": "2026-11-23"}
    # 2 回目 (同じ ASIN が再び新規に見えても重複して日付を書き換えない)
    assert tq.add(set(), {"B0NEW00001"}, TODAY + dt.timedelta(days=1), q, per) == []
    assert tq.load(q) == {"B0NEW00001": "2026-11-23"}


def test_expire_and_settle(tmp_path):
    q = tmp_path / "q.json"
    tq.save({"B0A0000001": "2026-11-01", "B0A0000002": "2026-11-20", "B0A0000003": "2026-11-21"}, q)
    # 11-01 は 14 日を過ぎて期限切れ。keep に無い 11-21 は外れる
    removed = tq.settle(q, {"B0A0000002"}, TODAY)
    assert removed == 2 and tq.load(q) == {"B0A0000002": "2026-11-20"}


def test_load_tolerates_broken_file(tmp_path):
    q = tmp_path / "q.json"
    q.write_text("{broken", encoding="utf-8")
    assert tq.load(q) == {}
    q.write_text(json.dumps({"asins": {"B0A0000001": "2026-11-23", "bad": "x", "B0A0000002": 5}}), encoding="utf-8")
    assert tq.load(q) == {"B0A0000001": "2026-11-23"}
