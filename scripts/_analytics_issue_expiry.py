"""_analytics_issue_expiry.py

A レーン検出器 (A-1〜A-5 / A-7, epic #1356) が per-URL で起票する Issue に
**有効期限**を埋め込むための共通ヘルパ。

## なぜ必要か

A レーンの Issue は「ある観測週のスナップショット」であって恒久課題ではない。
観測期間が過ぎれば数値は現況を表さなくなるが、これまでは open のまま残り続けて
いた (2026-09-02 に amazon-navi-brain で 3 件を手で棚卸しした)。古いスナップ
ショットが混ざると、

- どれが現況の候補か分からなくなる
- 同じ検出器の marker guard により、**新しい週の再検出が起票されない**
  (dedup は open な同 URL Issue を見て skip するため、古い 1 件が新しい観測を
  永久にブロックする)

の 2 つが起きる。後者のほうが実害が大きい。

## 期限の決め方

`source_range.end`（観測期間の終端日）+ `DEFAULT_TTL_DAYS`。
観測期間 + 2 週 = 週次 run 2 回分の猶予。手を付けなければ自動 close され、
まだ検出され続けているなら次の run が最新の数値で立て直す。

`source_range.end` が無い / 壊れている場合は marker を出さない (期限なし = 従来
どおり open のまま)。日付が読めないものを勝手に閉じるほうが危険なため。
"""
from __future__ import annotations

import datetime as _dt
import re

MARKER_PREFIX = "analytics-expires:"
DEFAULT_TTL_DAYS = 14

_MARKER_RE = re.compile(
    r"<!--\s*" + re.escape(MARKER_PREFIX) + r"\s*(\d{4}-\d{2}-\d{2})\s*-->"
)


def _parse_date(s: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return None


def expiry_date(src_range: dict | None, *, ttl_days: int = DEFAULT_TTL_DAYS) -> _dt.date | None:
    """観測期間の終端 + ttl_days。終端が読めなければ None。"""
    end = (src_range or {}).get("end")
    if not isinstance(end, str):
        return None
    d = _parse_date(end)
    if d is None:
        return None
    return d + _dt.timedelta(days=ttl_days)


def expiry_marker(src_range: dict | None, *, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """本文先頭に埋め込む HTML コメント。期限を決められなければ空文字。"""
    d = expiry_date(src_range, ttl_days=ttl_days)
    if d is None:
        return ""
    return f"<!-- {MARKER_PREFIX}{d.isoformat()} -->"


def expiry_note(src_range: dict | None, *, ttl_days: int = DEFAULT_TTL_DAYS) -> list[str]:
    """「## 自動運用」節に足す人間向けの行。期限が無ければ空リスト。"""
    d = expiry_date(src_range, ttl_days=ttl_days)
    if d is None:
        return []
    return [
        f"- **有効期限 {d.isoformat()}** (観測期間の終端 + {ttl_days} 日) — "
        "この日を過ぎると `close_expired_analytics_issues.py` が自動 close する。"
        "スナップショットであって恒久課題ではないため",
        "- 期限までに着手しない場合、まだ検出され続けているなら次週の run が"
        "最新の数値で立て直す (自動 close は取りこぼしではない)",
    ]


def find_expiry(body: str | None) -> _dt.date | None:
    """Issue 本文から期限を読む。marker が無ければ None。"""
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    return _parse_date(m.group(1))


def is_expired(body: str | None, *, today: _dt.date) -> bool:
    d = find_expiry(body)
    return d is not None and today > d
