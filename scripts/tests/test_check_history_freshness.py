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
    DIR_LANES,
    LANES,
    UNMONITORED,
    DirLane,
    Lane,
    check,
    check_dirs,
    last_date,
    last_date_in_dir,
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
    for lane in (*LANES, *DIR_LANES):
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


# ---------- DIR_LANES: ディレクトリ単位レーン (価格観測。#5015) ----------

DIR_LANE = (DirLane("data/price_watch/history", "daily", 3, "wf.yml"),)


def _write_ts(path: pathlib.Path, timestamps) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"ts": t, "source": "amazon", "price": 100})
                  for t in timestamps) + "\n",
        encoding="utf-8")


def test_last_date_reads_ts_field(tmp_path):
    """価格レーンは日付を ``date`` ではなく ``ts`` に持つ。"""
    p = tmp_path / "B1.jsonl"
    _write_ts(p, ["2026-08-05T21:10:00+00:00"])
    assert last_date(p) == D("2026-08-05")


def test_last_date_prefers_date_over_ts(tmp_path):
    """両方あるレコードでは既存の ``date`` を優先する (既存レーンの挙動を変えない)。"""
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps({"date": "2026-08-05", "ts": "2026-01-01T00:00:00+00:00"}) + "\n",
                 encoding="utf-8")
    assert last_date(p) == D("2026-08-05")


def test_last_date_in_dir_takes_max_across_files(tmp_path):
    """個別 ASIN は dedupe で何週も止まりうるので、代表値はディレクトリ全体の最新。"""
    root = tmp_path / "price_watch" / "history"
    _write_ts(root / "B1.jsonl", ["2026-07-01T00:00:00+00:00"])
    _write_ts(root / "B2.jsonl", ["2026-08-11T21:10:00+00:00"])
    _write_ts(root / "B3.jsonl", ["2026-08-02T21:10:00+00:00"])
    assert last_date_in_dir(root) == D("2026-08-11")


def test_last_date_in_dir_none_when_dir_absent(tmp_path):
    assert last_date_in_dir(tmp_path / "nope") is None


def test_dir_lane_ok_within_threshold(tmp_path):
    _write_ts(tmp_path / "data" / "price_watch" / "history" / "B1.jsonl",
              ["2026-08-11T21:10:00+00:00"])
    rows = check_dirs(tmp_path, D("2026-08-12"), DIR_LANE)
    assert [r["status"] for r in rows] == ["ok"]
    assert rows[0]["age_days"] == 1


def test_dir_lane_stale_when_lane_stops(tmp_path):
    """NAS runner が落ちて追記が止まったら鳴る (これまで検出できなかったケース)。"""
    _write_ts(tmp_path / "data" / "price_watch" / "history" / "B1.jsonl",
              ["2026-08-05T21:10:00+00:00"])
    rows = check_dirs(tmp_path, D("2026-08-12"), DIR_LANE)
    assert rows[0]["status"] == "stale"
    assert rows[0]["age_days"] == 7


def test_dir_lane_missing_when_dir_absent(tmp_path):
    rows = check_dirs(tmp_path, D("2026-08-12"), DIR_LANE)
    assert rows[0]["status"] == "missing"


def test_dir_lane_unknown_when_no_readable_date(tmp_path):
    """ディレクトリはあるが日付が 1 つも読めない状態を ok に潰さない。"""
    root = tmp_path / "data" / "price_watch" / "history"
    root.mkdir(parents=True)
    (root / "B1.jsonl").write_text("not json\n", encoding="utf-8")
    rows = check_dirs(tmp_path, D("2026-08-12"), DIR_LANE)
    assert rows[0]["status"] == "unknown"


def test_dir_lane_rows_render_like_file_lanes(tmp_path):
    _write_ts(tmp_path / "data" / "price_watch" / "history" / "B1.jsonl",
              ["2026-08-05T21:10:00+00:00"])
    rows = check_dirs(tmp_path, D("2026-08-12"), DIR_LANE)
    body = render_body(rows, [], D("2026-08-12"))
    assert "| `data/price_watch/history` | stale |" in body
    assert problems(rows, []) == ["data/price_watch/history"]


def test_registered_dir_lanes_cover_both_price_lanes():
    """価格 2 レーンが両方登録されていること (片方だけ足して安心しない)。"""
    assert {l.path for l in DIR_LANES} == {
        "data/price_watch/history", "data/price_history"}


def test_dir_lanes_do_not_fire_on_real_data_today():
    """導入時点の実データで鳴らない = 鳴りっぱなしゲートにしない (既存 LANES と同じ規律)。

    キャリブレーション (2026-08-12 replay): price_watch は 30 日窓で age 最大 1、
    price_history は 75 日窓で age 0 が 58 日 (2 以上は GitHub 凍結期間の 13 日欠測のみ)。
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    if not (repo_root / "data" / "price_watch" / "history").is_dir():
        pytest.skip("価格レーンのデータが無い環境")
    rows = check_dirs(repo_root, dt.datetime.now(dt.timezone.utc).date())
    assert [r["status"] for r in rows] == ["ok", "ok"], rows
