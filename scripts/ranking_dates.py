"""楽天ランキング履歴 (hugo/data/ranking/history.json) の MM-DD を実際の日付に戻す。

history の各点は年を持たない MM-DD で、年の前後は配列の挿入順 (= 古い順) で表す
(append_ranking_history.py の規約)。「今日より後の MM-DD は去年」と 1 点ずつ
決めると、1 年近く前の点 (例: 去年の 11-10) を今年の日付と取り違えるので、
末尾 (いちばん新しい点) から遡り、各点を「後ろの点の日付以前で最も新しい
その MM-DD」に当てはめる。2/29 はうるう年にだけ当てはまる。

Py3.8 の K8 runner からも import されうるので標準ライブラリの datetime だけを使う。
"""
from __future__ import annotations

import datetime
from typing import Any, List, Optional

JST = datetime.timezone(datetime.timedelta(hours=9))


def today_jst() -> datetime.date:
    """履歴の MM-DD は JST。UTC で動く CI でも JST の今日を基準にする。"""
    return datetime.datetime.now(JST).date()


def _latest_on_or_before(month: int, day: int, ref: datetime.date) -> Optional[datetime.date]:
    for year in range(ref.year, ref.year - 9, -1):
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:  # 平年の 02-29 など
            continue
        if candidate <= ref:
            return candidate
    return None


def resolve_entry_dates(entries: List[Any], today: datetime.date) -> List[Optional[datetime.date]]:
    """entries[i]["d"] (MM-DD) を日付に変換する。読めない点は None。"""
    resolved: List[Optional[datetime.date]] = [None] * len(entries)
    ref = today
    for i in range(len(entries) - 1, -1, -1):
        row = entries[i]
        try:
            month, day = (int(x) for x in str(row["d"]).split("-"))
        except (KeyError, TypeError, ValueError):
            continue
        found = _latest_on_or_before(month, day, ref)
        if found is None:
            continue
        resolved[i] = found
        ref = found
    return resolved
