"""#9449: hugo/data/sale_events.yaml (セール期間の告知の日程) の検証。

このファイルは人が手で書き、書いた内容がそのまま全ページのヘッダー下に出る。
壊れ方で一番困るのは **期間の書き間違いで、やっていないセールを「開催中」と出すこと**
(/deals/ が据え置き価格に「今セール中」と書いていた #6285 と同じクラス) なので、
日時の形と順序、確認元の記載を固定する。
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

SALE_EVENTS = Path(__file__).resolve().parents[2] / "hugo" / "data" / "sale_events.yaml"
JST = timedelta(hours=9)


def _events():
    return yaml.safe_load(SALE_EVENTS.read_text(encoding="utf-8")) or []


def _phases():
    return [
        pytest.param(event, phase, id=f"{event.get('id')}:{phase.get('label')}")
        for event in _events()
        for phase in event.get("phases") or []
    ]


def _parse(value):
    # クォート無しで書くと YAML が datetime に変換し、Hugo 側の jsonify の出力形式が
    # 変わる。文字列で持たせる。
    assert isinstance(value, str), f"日時はクォートして文字列で書く: {value!r}"
    return datetime.fromisoformat(value)


def test_top_level_is_a_list():
    assert isinstance(_events(), list)


def test_ids_are_unique():
    ids = [event.get("id") for event in _events()]
    assert all(ids), "id の無いイベントがある"
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("event", _events(), ids=lambda e: e.get("id"))
def test_event_has_name_source_and_phases(event):
    assert event.get("name")
    assert event.get("source"), "確認元 (source) を書く"
    verified_on = event.get("verified_on")
    if isinstance(verified_on, str):
        verified_on = date.fromisoformat(verified_on)
    assert isinstance(verified_on, date), "確認日 (verified_on) を YYYY-MM-DD で書く"
    assert event.get("phases"), "phases が空"
    assert all(isinstance(note, str) and note for note in event.get("notes") or [])


@pytest.mark.parametrize("event,phase", _phases())
def test_phase_has_jst_period_in_order(event, phase):
    assert phase.get("label")
    assert phase.get("headline")
    start, end = _parse(phase.get("start")), _parse(phase.get("end"))
    assert start.utcoffset() == JST and end.utcoffset() == JST, "+09:00 を付ける"
    assert start < end


@pytest.mark.parametrize("event", _events(), ids=lambda e: e.get("id"))
def test_phases_are_sorted_and_do_not_overlap(event):
    periods = [(_parse(p["start"]), _parse(p["end"])) for p in event["phases"]]
    for (_, prev_end), (next_start, _) in zip(periods, periods[1:]):
        assert prev_end < next_start, "phases は時系列順に書き、重ねない"
