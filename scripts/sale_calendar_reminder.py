#!/usr/bin/env python3
"""毎年来るセールの時期が近づいたら、日程登録の issue を 1 件立てる (#9449)。

セール期間の告知 (hugo/data/sale_events.yaml) は、人が公式の告知で日程を確認して
1 件追記する運用。仕組みとしては回るが、**追記を思い出す手段が無い**ので、
忘れるとそのセールは告知されないまま過ぎる。

このスクリプトは data/sale_calendar.yaml (例年の目安の時期) を読み、開始の目安が
近いのに sale_events.yaml に未登録のセールがあれば、登録を促す issue を立てる。

決めごと:
  - **外部サイトは取りに行かない。** 公式の告知は機械可読でなく、取り違えると
    やっていないセールを「開催中」と出すことになる (#6285 と同じ形)。日程の確認と
    登録は人がやる。ここがやるのは「そろそろ確認する時期」と知らせることだけ
  - **同じセールで 2 回立てない。** 本文のマーカー (id と年) で、open / closed を
    問わず既存の issue を探す。closed も数えるのは、「今年は登録しない」と人が
    閉じた判断を、翌週の run が覆さないため
  - **1 回の run で立てるのは 1 件まで。** 時期の重なるセールがあっても、残りは
    次の週の run に回す (issue の一括起票をしない)
  - 既存 issue の検索に失敗したら、立てずに非ゼロで終わる。確認できないまま立てると
    重複するし、緑で終わると壊れていることに気づけない
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._marker_issue import marker_comment, verified_matches  # noqa: E402

logger = logging.getLogger("sale_calendar_reminder")

CALENDAR = ROOT / "data" / "sale_calendar.yaml"
SALE_EVENTS = ROOT / "hugo" / "data" / "sale_events.yaml"
MARKER_PREFIX = "sale-calendar-reminder"
LABELS = "todo"
JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_REMIND_DAYS = 21
# 目安の開始日を過ぎても、この日数までは未登録なら知らせる。目安は例年の早い方に
# 寄せてあり、実際の開催は 2 週間ほど後ろにずれることがあるため
GRACE_DAYS = 28
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MMDD_RE = re.compile(r"^(\d{2})-(\d{2})$")


class CalendarError(ValueError):
    """data/sale_calendar.yaml の書き方の誤り。"""


def load_yaml(path: pathlib.Path) -> List[Dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise CalendarError("{} の最上位はリストにする".format(path.name))
    return data


def validate_calendar(entries: Sequence[Dict[str, Any]]) -> None:
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CalendarError("各項目はマップにする: {!r}".format(entry))
        cal_id = entry.get("id")
        if not isinstance(cal_id, str) or not ID_RE.match(cal_id):
            raise CalendarError("id は英小文字・数字・ハイフン: {!r}".format(cal_id))
        if cal_id in seen:
            raise CalendarError("id が重複している: {}".format(cal_id))
        seen.add(cal_id)
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise CalendarError("{}: name が無い".format(cal_id))
        typical_date(entry, 2024)  # うるう年で形だけ確かめる
        days = entry.get("remind_days_before", DEFAULT_REMIND_DAYS)
        # bool は int の部分型なので先に弾く。上限は、前年の同じセールの時期まで
        # さかのぼって立てないため
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise CalendarError(
                "{}: remind_days_before は 1〜90 の整数: {!r}".format(cal_id, days))


def typical_date(entry: Dict[str, Any], year: int) -> dt.date:
    raw = entry.get("typical_start")
    m = MMDD_RE.match(raw) if isinstance(raw, str) else None
    if not m:
        raise CalendarError(
            '{}: typical_start は "MM-DD" の文字列: {!r}'.format(entry.get("id"), raw))
    month, day = int(m.group(1)), int(m.group(2))
    if (month, day) == (2, 29):
        raise CalendarError("{}: 02-29 は毎年来ないので書かない".format(entry.get("id")))
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise CalendarError("{}: typical_start が日付でない: {!r}".format(
            entry.get("id"), raw)) from exc


def registered_ids(events: Sequence[Dict[str, Any]]) -> set:
    return {e.get("id") for e in events if isinstance(e, dict) and isinstance(e.get("id"), str)}


def due_reminders(
    calendar: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    today: dt.date,
) -> List[Dict[str, Any]]:
    """今日の時点で知らせるべき (時期が近い・未登録の) セールを、目安の早い順に返す。"""
    validate_calendar(calendar)
    registered = registered_ids(events)
    due = []
    for entry in calendar:
        lead = entry.get("remind_days_before", DEFAULT_REMIND_DAYS)
        # 年またぎ (1 月のセールを 12 月に知らせる) があるので、今年と来年の両方を見る
        for year in (today.year, today.year + 1):
            start = typical_date(entry, year)
            if not start - dt.timedelta(days=lead) <= today <= start + dt.timedelta(days=GRACE_DAYS):
                continue
            event_id = "{}-{}".format(entry["id"], year)
            if event_id in registered:
                continue
            due.append({
                "event_id": event_id,
                "calendar_id": entry["id"],
                "name": entry["name"],
                "year": year,
                "typical_start": start,
                "note": entry.get("note") or "",
            })
    return sorted(due, key=lambda r: (r["typical_start"], r["event_id"]))


def marker_for(event_id: str) -> str:
    return "{}:{}".format(MARKER_PREFIX, event_id)


def title_for(row: Dict[str, Any]) -> str:
    return "[sale] {} {} の日程を確認して告知に登録する".format(row["name"], row["year"])


def render_body(row: Dict[str, Any], today: dt.date) -> str:
    start = row["typical_start"]
    lines = [
        marker_comment(marker_for(row["event_id"])),
        "",
        "**{} {}** の時期が近づいています（例年の開始の目安: {}/{} ごろ）。".format(
            row["name"], row["year"], start.month, start.day),
        "",
        "この目安は過去の開催時期からの概算で、今年の日程ではありません。"
        "公式の告知で日程を確認してから登録してください。",
        "",
        "## やること",
        "",
        "1. Amazon.co.jp の公式の告知で、開催期間（先行セールがあればその期間も）を確認する",
        "2. `hugo/data/sale_events.yaml` に 1 件追記する PR を出す",
        "   - `id` は **`{}`** にする（この id で登録済みかを判定しています）".format(
            row["event_id"]),
        "   - `source` と `verified_on`（確認元と確認日）を書く",
        "   - 書き方はファイル冒頭のコメントと `CLAUDE.md` の「セール期間の告知を出す」",
        "3. `python -m pytest scripts/tests/test_sale_events.py` を通す",
        "4. マージしたらこの issue を閉じる",
        "",
        "まだ告知が出ていなければ、出るまで開けたままにしてください。"
        "今年は登録しないと決めたら、そのまま閉じてください（閉じた issue は立て直しません）。",
        "",
        "## 注意",
        "",
        "- 告知に書いてよいのは「セール期間中である」ことまで。"
        "個別の商品が値引きされているとは書かない (#6285)",
        "- 日程は自動では取得していません。未確認の日程を登録しないこと",
    ]
    if row["note"]:
        lines += ["", "メモ: {}".format(row["note"])]
    lines += [
        "",
        "---",
        "`data/sale_calendar.yaml` と `scripts/sale_calendar_reminder.py` による"
        "自動起票（{} 判定）。Refs #9449".format(today.isoformat()),
    ]
    return "\n".join(lines) + "\n"


# --- GitHub 側 (check_delivery_freshness.py と同じ作法) ----------------------

def _gh(args: List[str]) -> str:
    import subprocess
    # encoding を明示する (Windows の既定 cp932 だと日本語の応答で壊れる)
    res = subprocess.run(["gh", *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return res.stdout


def find_existing_issue(repo: str, event_id: str) -> Optional[int]:
    """このセールのリマインド issue (open / closed とも) があれば番号を返す。"""
    marker = marker_for(event_id)
    query = 'repo:{} is:issue in:body "{}"'.format(repo, marker)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=20"])
    items = json.loads(out).get("items", [])
    # 検索結果は候補でしかない。マーカーコメントの実体で検証してから採用する (#6622)
    matches = verified_matches(items, marker)
    return matches[0]["number"] if matches else None


def create_issue(repo: str, title: str, body: str) -> str:
    return _gh(["issue", "create", "-R", repo, "--title", title,
                "--label", LABELS, "--body", body]).strip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--today", default=None,
                   help="判定基準日 (YYYY-MM-DD, 既定: JST の今日)。検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="issue を立てず、判定結果と本文だけ出す")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(JST).date())
    due = due_reminders(load_yaml(CALENDAR), load_yaml(SALE_EVENTS), today)
    logger.info("today=%s due=%s", today.isoformat(),
                [r["event_id"] for r in due] or "none")

    created = False
    for row in due:
        if args.repo:
            existing = find_existing_issue(args.repo, row["event_id"])
            if existing:
                logger.info("%s: issue #%s が既にあるので立てない", row["event_id"], existing)
                continue
        elif not args.dry_run:
            logger.error("--repo (または環境変数 REPO) が無いと issue を立てられない")
            return 2
        if created:
            logger.info("%s: 1 run で立てるのは 1 件まで。次の run に回す", row["event_id"])
            continue
        title, body = title_for(row), render_body(row, today)
        if args.dry_run:
            logger.info("[dry-run] 立てる issue: %s\n%s", title, body)
        else:
            logger.info("%s: issue を立てた %s", row["event_id"],
                        create_issue(args.repo, title, body))
        created = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
