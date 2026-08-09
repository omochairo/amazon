"""check_history_freshness.py の unit tests (#4789).

成り立つべき条件:
  1. 導入時点 (2026-08-09) の実データで **1 件も鳴らない** = 鳴りっぱなしゲートにしない。
  2. 止まったら必ず鳴る (secret 無言 no-op / commit-back 握りつぶし / lane 未起動の
     どれで止まっても「最終計測日が進まない」に落ちるので同じ網で拾える)。
  3. 判定不能 (空 / 壊れている / 監視表に無い) を ok に潰さない。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scripts.check_history_freshness import (
    LANES,
    UNMONITORED,
    Lane,
    check,
    last_date,
    problems,
    render_body,
    unregistered_files,
)

D = dt.date.fromisoformat


def _write(path: pathlib.Path, dates) -> None:
    path.write_text(
        "\n".join(json.dumps({"date": d, "url": "https://x/"}) for d in dates) + "\n",
        encoding="utf-8")


@pytest.fixture()
def hist(tmp_path):
    d = tmp_path / "history"
    d.mkdir()
    return d


# ---------- last_date ----------

def test_last_date_takes_max_not_last_line(hist):
    # #4772 の後勝ち append で行順は時系列と一致しない
    p = hist / "a.jsonl"
    _write(p, ["2026-08-08", "2026-08-05", "2026-08-07"])
    assert last_date(p) == D("2026-08-08")


def test_last_date_skips_broken_lines(hist):
    p = hist / "a.jsonl"
    p.write_text('{"date": "2026-08-05"}\nnot json\n{"nodate": 1}\n[]\n\n',
                 encoding="utf-8")
    assert last_date(p) == D("2026-08-05")


def test_last_date_none_when_no_dates(hist):
    p = hist / "a.jsonl"
    p.write_text('{"nodate": 1}\n', encoding="utf-8")
    assert last_date(p) is None


def test_last_date_none_when_missing(hist):
    assert last_date(hist / "nope.jsonl") is None


def test_last_date_accepts_timestamp_prefix(hist):
    p = hist / "a.jsonl"
    _write(p, ["2026-08-05T10:00:00+09:00"])
    assert last_date(p) == D("2026-08-05")


# ---------- check ----------

LANE = (Lane("a.jsonl", "daily", 3, "wf.yml"),)


def test_within_threshold_is_ok(hist):
    _write(hist / "a.jsonl", ["2026-08-08"])
    rows = check(hist, D("2026-08-09"), LANE)
    assert rows[0]["status"] == "ok"
    assert rows[0]["age_days"] == 1


def test_exactly_at_threshold_is_ok(hist):
    _write(hist / "a.jsonl", ["2026-08-06"])
    assert check(hist, D("2026-08-09"), LANE)[0]["status"] == "ok"


def test_beyond_threshold_is_stale(hist):
    _write(hist / "a.jsonl", ["2026-08-05"])
    row = check(hist, D("2026-08-09"), LANE)[0]
    assert row["status"] == "stale"
    assert row["age_days"] == 4


def test_missing_file_is_missing_not_ok(hist):
    assert check(hist, D("2026-08-09"), LANE)[0]["status"] == "missing"


def test_undatable_file_is_unknown_not_ok(hist):
    """空 / 日付なしを ok に潰さない (unknown を pass にしない)。"""
    (hist / "a.jsonl").write_text("", encoding="utf-8")
    assert check(hist, D("2026-08-09"), LANE)[0]["status"] == "unknown"


def test_future_date_is_not_stale(hist):
    # 論理日 (#4785) の都合で当日分が先に入ることがある
    _write(hist / "a.jsonl", ["2026-08-10"])
    assert check(hist, D("2026-08-09"), LANE)[0]["status"] == "ok"


# ---------- unregistered ----------

def test_unregistered_file_is_reported(hist):
    _write(hist / "a.jsonl", ["2026-08-09"])
    _write(hist / "brand_new_lane.jsonl", ["2026-08-09"])
    assert unregistered_files(hist, LANE) == ["brand_new_lane.jsonl"]


def test_unmonitored_files_are_not_unregistered(hist):
    for name in UNMONITORED:
        _write(hist / name, ["2026-08-09"])
    assert unregistered_files(hist, LANE) == []


def test_problems_is_empty_when_all_ok(hist):
    _write(hist / "a.jsonl", ["2026-08-09"])
    rows = check(hist, D("2026-08-09"), LANE)
    assert problems(rows, unregistered_files(hist, LANE)) == []


def test_problems_counts_unregistered_too(hist):
    _write(hist / "a.jsonl", ["2026-08-09"])
    _write(hist / "x.jsonl", ["2026-08-09"])
    rows = check(hist, D("2026-08-09"), LANE)
    assert problems(rows, unregistered_files(hist, LANE)) == ["x.jsonl"]


# ---------- 較正: 導入時点の実データで鳴らない ----------

def test_real_history_is_quiet_on_introduction_date():
    """2026-08-09 の実 main の履歴で 1 件も鳴らないこと (#4789 の較正根拠)。

    実測経過: ga4=1d / gsc=4d / gsc_wp=5d / lighthouse=1d / census=7d / crux=3d。
    ここが赤くなったら、閾値が実運用の遅延に対して厳しすぎるか、
    本当にレーンが止まっているかのどちらか。
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    hist = repo_root / "data" / "analytics" / "history"
    if not hist.is_dir():  # pragma: no cover - チェックアウト構成が違うときは skip
        pytest.skip("history dir not present")
    rows = check(hist, D("2026-08-09"))
    bad = [r for r in rows if r["status"] != "ok"]
    assert bad == [], [(r["filename"], r["status"], r["last"]) for r in bad]


def test_real_history_all_lanes_registered():
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    hist = repo_root / "data" / "analytics" / "history"
    if not hist.is_dir():  # pragma: no cover
        pytest.skip("history dir not present")
    assert unregistered_files(hist) == []


def test_thresholds_have_headroom_over_cadence():
    """cadence より短い上限を置かない (毎回鳴るゲートを作らない)。"""
    floor = {"daily": 2, "weekly": 8, "monthly": 32}
    for lane in LANES:
        assert lane.max_age_days >= floor[lane.cadence], lane.filename


# ---------- body ----------

def test_body_carries_marker_and_stale_row(hist):
    _write(hist / "a.jsonl", ["2026-08-01"])
    rows = check(hist, D("2026-08-09"), LANE)
    body = render_body(rows, [], D("2026-08-09"))
    assert "<!-- history-freshness-monitor -->" in body
    assert "`a.jsonl`" in body
    assert "wf.yml" in body


def test_body_lists_unregistered_section(hist):
    _write(hist / "a.jsonl", ["2026-08-09"])
    rows = check(hist, D("2026-08-09"), LANE)
    body = render_body(rows, ["mystery.jsonl"], D("2026-08-09"))
    assert "mystery.jsonl" in body
    # 健全なレーンは折り畳みに入れて本題を埋めない
    assert "<details>" in body


def test_body_headline_matches_content_when_only_unregistered(hist):
    """stale 0 件 + 未登録ありのとき「0 件」と書かない (本文と矛盾させない)。"""
    _write(hist / "a.jsonl", ["2026-08-09"])
    rows = check(hist, D("2026-08-09"), LANE)
    body = render_body(rows, ["mystery.jsonl"], D("2026-08-09"))
    assert "計 1 件" in body
    assert "止まっているレーン **" not in body   # 空テーブルを出さない
    assert "監視表に無い履歴ファイル **1 件**" in body


def test_body_headline_counts_both_kinds(hist):
    _write(hist / "a.jsonl", ["2026-08-01"])
    rows = check(hist, D("2026-08-09"), LANE)
    body = render_body(rows, ["mystery.jsonl"], D("2026-08-09"))
    assert "計 2 件" in body
    assert "| `a.jsonl` | stale |" in body
