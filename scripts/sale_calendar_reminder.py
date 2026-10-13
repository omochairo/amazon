#!/usr/bin/env python3
"""毎年来るセールの時期が近づいたら、日程登録の issue を 1 件立てる (#9449)。

セール期間の告知 (hugo/data/sale_events.yaml) は、人が公式の告知で日程を確認して
1 件追記する運用。仕組みとしては回るが、**追記を思い出す手段が無い**ので、
忘れるとそのセールは告知されないまま過ぎる。

このスクリプトは data/sale_calendar.yaml (例年の目安の時期) を読み、開始の目安が
近いのに sale_events.yaml に未登録のセールがあれば、登録を促す issue を立てる。

あわせて公式のフィード 2 本を読み、開催の発表が出ていたら、題名から読んだ日程を
「候補」として issue に載せる (scripts/sale_announcements.py)。

決めごと:
  - **日程は自動では登録しない。** 題名の読み違いや、日付の違う関連の発表との
    取り違えがあると、やっていないセールを「開催中」と出すことになる (#6285 と
    同じ形)。候補は issue に載せるだけで、出典での確認と登録は人がやる
  - **発表を見つけたら、目安の時期を待たずに知らせる。** 既に目安で立てた issue が
    開いていれば、そこへ 1 回だけコメントする。閉じた issue には何もしない
  - **フィードを読めなかったら、できる仕事 (目安によるリマインド) を済ませてから
    非ゼロで終わる。** 緑で終わると、検知が止まっていることに気づけない
  - **同じセールで 2 回立てない。** 本文のマーカー (id と年) で、open / closed を
    問わず既存の issue を探す。closed も数えるのは、「今年は登録しない」と人が
    閉じた判断を、翌週の run が覆さないため
  - **1 回の run で書くのは 1 件まで (起票かコメントのどちらか)。** 時期の重なる
    セールがあっても、残りは次の run に回す (issue の一括起票をしない)
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

from scripts import sale_announcements  # noqa: E402
from scripts._marker_issue import marker_comment, verified_matches  # noqa: E402

logger = logging.getLogger("sale_calendar_reminder")

CALENDAR = ROOT / "data" / "sale_calendar.yaml"
SALE_EVENTS = ROOT / "hugo" / "data" / "sale_events.yaml"
MARKER_PREFIX = "sale-calendar-reminder"
# 発表の候補を載せた印。本文かコメントにあれば、同じセールで 2 回載せない
ANNOUNCE_MARKER_PREFIX = "sale-announcement"
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
        names = entry.get("announce_names", [])
        if not isinstance(names, list) or not all(
                isinstance(n, str) and n.strip() for n in names):
            raise CalendarError(
                "{}: announce_names は空でない文字列のリスト: {!r}".format(cal_id, names))
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


def announce_marker_for(event_id: str) -> str:
    return "{}:{}".format(ANNOUNCE_MARKER_PREFIX, event_id)


def title_for(row: Dict[str, Any]) -> str:
    if row.get("candidate"):
        return "[sale] {} {} の開催が発表された — 日程を確認して告知に登録する".format(
            row["name"], row["year"])
    return "[sale] {} {} の日程を確認して告知に登録する".format(row["name"], row["year"])


def render_body(row: Dict[str, Any], today: dt.date) -> str:
    candidate = row.get("candidate")
    lines = [marker_comment(marker_for(row["event_id"]))]
    if candidate:
        lines += [
            marker_comment(announce_marker_for(row["event_id"])),
            "",
            "**{} {}** の開催が発表されたようです（自動検知・未確認）。".format(
                row["name"], row["year"]),
            "",
            *sale_announcements.render_candidate(candidate),
            "",
            "## やること",
            "",
            "1. 上の出典を開いて、開催期間（先行セールがあればその期間も）を確認する",
        ]
    else:
        start = row["typical_start"]
        lines += [
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
        ]
    lines += [
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
        "- 日程は自動では登録しません。出典で確かめていない日程を登録しないこと",
    ]
    if row.get("note"):
        lines += ["", "メモ: {}".format(row["note"])]
    lines += [
        "",
        "---",
        "`data/sale_calendar.yaml` と `scripts/sale_calendar_reminder.py` による"
        "自動起票（{} 判定）。Refs #9449".format(today.isoformat()),
    ]
    return "\n".join(lines) + "\n"


def render_comment(row: Dict[str, Any], today: dt.date) -> str:
    """目安で立てた issue が既に開いているときに足す、発表の候補のコメント。"""
    lines = [
        marker_comment(announce_marker_for(row["event_id"])),
        "",
        "**{} {}** の開催が発表されたようです（自動検知・未確認）。".format(
            row["name"], row["year"]),
        "",
        *sale_announcements.render_candidate(row["candidate"]),
        "",
        "---",
        "`scripts/sale_calendar_reminder.py` による自動コメント（{} 判定）。".format(
            today.isoformat()),
    ]
    return "\n".join(lines) + "\n"


# --- GitHub 側 (check_delivery_freshness.py と同じ作法) ----------------------

def _gh(args: List[str]) -> str:
    import subprocess
    # encoding を明示する (Windows の既定 cp932 だと日本語の応答で壊れる)
    res = subprocess.run(["gh", *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return res.stdout


def find_existing(repo: str, event_id: str) -> Optional[Dict[str, Any]]:
    """このセールのリマインド issue (open / closed とも) があれば返す。"""
    marker = marker_for(event_id)
    query = 'repo:{} is:issue in:body "{}"'.format(repo, marker)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=20"])
    items = json.loads(out).get("items", [])
    # 検索結果は候補でしかない。マーカーコメントの実体で検証してから採用する (#6622)
    matches = verified_matches(items, marker)
    return matches[0] if matches else None


def announcement_posted(repo: str, issue: Dict[str, Any], event_id: str) -> bool:
    """発表の候補を、この issue の本文かコメントに既に載せたか。"""
    needle = marker_comment(announce_marker_for(event_id))
    if needle in (issue.get("body") or ""):
        return True
    out = _gh(["api", "-X", "GET",
               "repos/{}/issues/{}/comments".format(repo, issue["number"]),
               "-f", "per_page=100"])
    return any(needle in (c.get("body") or "") for c in json.loads(out))


def create_comment(repo: str, number: int, body: str) -> str:
    return _gh(["issue", "comment", str(number), "-R", repo, "--body", body]).strip()


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
    calendar, events = load_yaml(CALENDAR), load_yaml(SALE_EVENTS)
    due = due_reminders(calendar, events, today)
    detected, feed_errors = sale_announcements.collect(
        calendar, registered_ids(events), today)
    # 発表が出ているセールを先に。目安だけのセールはその後
    rows = [{"event_id": c["event_id"], "calendar_id": c["calendar_id"],
             "name": c["name"], "year": c["year"], "candidate": c}
            for _, c in sorted(detected.items())]
    rows += [r for r in due if r["event_id"] not in detected]
    logger.info("today=%s detected=%s due=%s", today.isoformat(),
                sorted(detected) or "none", [r["event_id"] for r in due] or "none")

    written = False
    for row in rows:
        event_id = row["event_id"]
        existing = None
        if args.repo:
            existing = find_existing(args.repo, event_id)
        elif not args.dry_run:
            logger.error("--repo (または環境変数 REPO) が無いと issue を立てられない")
            return 2
        comment = False
        if existing:
            if not row.get("candidate"):
                logger.info("%s: issue #%s が既にあるので立てない", event_id, existing["number"])
                continue
            if existing.get("state") != "open":
                logger.info("%s: issue #%s は閉じているので何もしない",
                            event_id, existing["number"])
                continue
            if announcement_posted(args.repo, existing, event_id):
                logger.info("%s: issue #%s に発表を載せ済み", event_id, existing["number"])
                continue
            comment = True
        if written:
            logger.info("%s: 1 run で書くのは 1 件まで。次の run に回す", event_id)
            continue
        if comment:
            body = render_comment(row, today)
            if args.dry_run:
                logger.info("[dry-run] issue #%s へのコメント:\n%s", existing["number"], body)
            else:
                logger.info("%s: issue #%s にコメントした %s", event_id, existing["number"],
                            create_comment(args.repo, existing["number"], body))
        else:
            title, body = title_for(row), render_body(row, today)
            if args.dry_run:
                logger.info("[dry-run] 立てる issue: %s\n%s", title, body)
            else:
                logger.info("%s: issue を立てた %s", event_id,
                            create_issue(args.repo, title, body))
        written = True

    if feed_errors:
        for err in feed_errors:
            logger.error("::error::フィードを読めなかった: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
