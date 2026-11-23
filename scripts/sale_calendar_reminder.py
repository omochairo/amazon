#!/usr/bin/env python3
"""毎年来るセールの時期が近づいたら、日程登録の issue を 1 件立てる (#9449)。

セール期間の告知 (hugo/data/sale_events.yaml) は、人が公式の告知で日程を確認して
1 件追記する運用。仕組みとしては回るが、**追記を思い出す手段が無い**ので、
忘れるとそのセールは告知されないまま過ぎる。

このスクリプトは data/sale_calendar.yaml (例年の目安の時期) を読み、開始の目安が
近いのに sale_events.yaml に未登録のセールがあれば、登録を促す issue を立てる。

あわせて公式のフィード 2 本と報道のフィード 1 本を読み、開催の発表が出ていたら、
題名から読んだ日程を「候補」として issue に載せる (scripts/sale_announcements.py)。
カレンダーに無いセール (月例のスマイルSALE の別名・単発のセールなど) の発表も拾う。

決めごと:
  - **このスクリプトは日程を登録しない。** 題名の読み違いや、日付の違う関連の発表との
    取り違えがあると、やっていないセールを「開催中」と出すことになる (#6285 と
    同じ形)。自動で登録するのは scripts/shop_sale_watch.py で、関門 (公式のフィード・
    日程が 1 つに定まる・開始と終了の日時が題名に全部ある) を通った候補だけ
    (2026-10-29 K 決定)。通らなかった候補はここで issue に載せ、人が確かめて登録する
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
# 自動登録した本番に先行セールを足す確認の印 (navi-brain#206 R1-7)
PRESALE_MARKER_PREFIX = "sale-presale"
AUTO_MARK = "shop_sale_watch"
LABELS = "todo"
JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_REMIND_DAYS = 21
# 目安の開始日を過ぎても、この日数までは未登録なら知らせる。目安は例年の早い方に
# 寄せてあり、実際の開催は 2 週間ほど後ろにずれることがあるため
GRACE_DAYS = 28
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MMDD_RE = re.compile(r"^(\d{2})-(\d{2})$")
CYCLES = ("yearly", "monthly")
SHOP_SITES = {"amazon": "Amazon.co.jp", "rakuten": "楽天市場", "yahoo": "Yahoo!ショッピング"}


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
        if entry.get("shop", "amazon") not in sale_announcements.SHOPS:
            raise CalendarError("{}: shop は {} のどれか: {!r}".format(
                cal_id, " / ".join(sale_announcements.SHOPS), entry.get("shop")))
        if not isinstance(entry.get("pre_sale", False), bool):
            raise CalendarError("{}: pre_sale は true / false".format(cal_id))
        url = entry.get("url")
        if url is not None and not (isinstance(url, str) and url.startswith("https://")):
            raise CalendarError("{}: url は https:// で始まる文字列: {!r}".format(cal_id, url))
        cycle = entry.get("cycle", "yearly")
        if cycle not in CYCLES:
            raise CalendarError("{}: cycle は {} のどれか: {!r}".format(
                cal_id, " / ".join(CYCLES), cycle))
        if cycle == "monthly":
            # 毎月のセールは目安の日が決まらないので、発表を拾ったときだけ知らせる
            if "typical_start" in entry or "remind_days_before" in entry:
                raise CalendarError(
                    "{}: cycle: monthly には typical_start / remind_days_before を書かない".format(
                        cal_id))
            continue
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


def registered_spans(events: Sequence[Dict[str, Any]]) -> List[tuple]:
    """登録済みのイベントの期間 (最初の区分の開始日〜最後の区分の終了日, JST) とモール。

    読めない区分は飛ばす (書き方の誤りは test_sale_events.py が落とす)。
    """
    spans = []
    for event in events:
        if not isinstance(event, dict):
            continue
        days = []
        for phase in event.get("phases") or []:
            for key in ("start", "end"):
                try:
                    days.append(dt.datetime.fromisoformat(str(phase[key])).astimezone(JST).date())
                except (KeyError, TypeError, ValueError):
                    pass
        if days:
            spans.append((min(days), max(days), event.get("shop") or "amazon"))
    return spans


def registered_events(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """登録済みのイベントを、検知の突き合わせに使う形にする (navi-brain#206)。

    {id, shop, name, auto, lo, hi, starts, phases}。lo / hi は最初の区分の開始日〜最後の
    区分の終了日 (JST)、starts は各区分の開始日。読めない区分は飛ばす。
    """
    out = []
    for event in events:
        if not isinstance(event, dict):
            continue
        starts, days = [], []
        for phase in event.get("phases") or []:
            for key in ("start", "end"):
                try:
                    day = dt.datetime.fromisoformat(str(phase[key])).astimezone(JST).date()
                except (KeyError, TypeError, ValueError):
                    continue
                days.append(day)
                if key == "start":
                    starts.append(day)
        if not days:
            continue
        out.append({"id": event.get("id"), "shop": event.get("shop") or "amazon",
                    "name": str(event.get("name") or ""), "auto": event.get("auto"),
                    "lo": min(days), "hi": max(days), "starts": starts,
                    "phases": len(event.get("phases") or [])})
    return out


def _registered_like(entry: Dict[str, Any], start: dt.date, lead: int,
                     infos: Sequence[Dict[str, Any]]) -> bool:
    """カレンダーのセールが、別の id で登録済みか (62 の自動登録の id は開始日入り)。

    同じモールで、どれかの段階の開始日が [目安 − lead, 目安 + GRACE_DAYS] にあり、
    名前が同じセール (sale_announcements.same_sale) のイベントがあれば登録済み。
    """
    lo, hi = start - dt.timedelta(days=lead), start + dt.timedelta(days=GRACE_DAYS)
    names = [entry["name"]] + list(entry.get("announce_names") or [])
    shop = sale_announcements.shop_of(entry)
    return any(info["shop"] == shop and any(lo <= s <= hi for s in info["starts"])
               and any(sale_announcements.same_sale(n, info["name"]) for n in names)
               for info in infos)


def due_reminders(
    calendar: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    today: dt.date,
) -> List[Dict[str, Any]]:
    """今日の時点で知らせるべき (時期が近い・未登録の) セールを、目安の早い順に返す。"""
    validate_calendar(calendar)
    registered = registered_ids(events)
    infos = registered_events(events)
    due = []
    for entry in calendar:
        if entry.get("cycle") == "monthly":
            continue
        lead = entry.get("remind_days_before", DEFAULT_REMIND_DAYS)
        # 年またぎ (1 月のセールを 12 月に知らせる) があるので、今年と来年の両方を見る
        for year in (today.year, today.year + 1):
            start = typical_date(entry, year)
            if not start - dt.timedelta(days=lead) <= today <= start + dt.timedelta(days=GRACE_DAYS):
                continue
            event_id = "{}-{}".format(entry["id"], year)
            if event_id in registered or _registered_like(entry, start, lead, infos):
                continue
            due.append({
                "event_id": event_id,
                "calendar_id": entry["id"],
                "shop": sale_announcements.shop_of(entry),
                "name": entry["name"],
                "year": year,
                "typical_start": start,
                "note": entry.get("note") or "",
                "url": entry.get("url") or "",
            })
    return sorted(due, key=lambda r: (r["typical_start"], r["event_id"]))


def presale_checks(
    calendar: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    today: dt.date,
) -> List[Dict[str, Any]]:
    """自動登録した本番に、先行セールを足す確認が要るもの (navi-brain#206 R1-7)。

    62 は題名から本番の期間だけを登録する (先行セールの日程は本文にしか無い)。本番が
    自動登録されると目安のリマインドも立たなくなり、人が先行セールを確かめる経路が消える。
    対象は、カレンダーで pre_sale: true のセールの「<id>-<年>」のイベントで、auto が付き、
    段階が 1 つで、本番がまだ始まっていないもの。人が先行セールを足して auto を消せば外れる。
    """
    pre = {e["id"]: e for e in calendar if e.get("pre_sale")}
    rows = []
    for info in registered_events(events):
        if info["auto"] != AUTO_MARK or info["phases"] != 1 or not info["starts"]:
            continue
        m = re.match(r"^(.+)-(\d{4})$", str(info["id"] or ""))
        if not m or m.group(1) not in pre or not today < info["starts"][0]:
            continue
        entry = pre[m.group(1)]
        rows.append({"event_id": info["id"], "calendar_id": entry["id"], "presale": True,
                     "shop": sale_announcements.shop_of(entry), "name": entry["name"],
                     "year": int(m.group(2)), "start": info["starts"][0],
                     "note": entry.get("note") or ""})
    return sorted(rows, key=lambda r: (r["start"], r["event_id"]))


def marker_for(event_id: str) -> str:
    return "{}:{}".format(MARKER_PREFIX, event_id)


def announce_marker_for(event_id: str) -> str:
    return "{}:{}".format(ANNOUNCE_MARKER_PREFIX, event_id)


def presale_marker_for(event_id: str) -> str:
    return "{}:{}".format(PRESALE_MARKER_PREFIX, event_id)


def title_for(row: Dict[str, Any]) -> str:
    if row.get("presale"):
        return "[sale] {} {} の先行セールの有無を確かめる（本番だけ自動登録済み）".format(
            row["name"], row["year"])
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
            # 決定 2 (navi-brain#206): 見つからないのを赤にはせず、この issue で知らせる
            "公式の発表の自動検知・キャンペーンページの自動登録は、まだこのセールを"
            "拾っていません（開催の発表が無い・読み取りの対象外・まだ発表前のどれか）。",
            "",
            "## やること",
            "",
            "1. {} の公式の告知{}で、開催期間（先行セールがあればその期間も）を確認する".format(
                SHOP_SITES[row.get("shop", "amazon")],
                "（{}）".format(row["url"]) if row.get("url") else ""),
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
        "今回は登録しないと決めたら、そのまま閉じてください（閉じた issue は立て直しません）。",
        "",
        "## 注意",
        "",
        "- 告知に書いてよいのは「セール期間中である」ことまで。"
        "個別の商品が値引きされているとは書かない (#6285)",
        "- 日程は出典で確かめてから登録すること"
        + ("（この候補は自動登録の関門〈公式のフィード・日時が全部題名にある〉を"
           "通らなかったので、自動では登録されません）" if candidate else ""),
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


def _presale_lines(row: Dict[str, Any]) -> List[str]:
    start = row["start"]
    return [
        "**{} {}** の本番（{}/{} から）は、公式の発表の題名から自動で登録されました。"
        "**先行セールは題名に出ないので、自動では登録されていません。**".format(
            row["name"], row["year"], start.month, start.day),
        "",
        "## やること",
        "",
        "1. 公式の発表の本文か {} の告知で、先行セールの有無と期間を確かめる".format(
            SHOP_SITES[row.get("shop", "amazon")]),
        "2. 先行セールがあれば、`hugo/data/sale_events.yaml` の **`{}`** の `phases` の"
        "先頭に段階を足し、`auto: shop_sale_watch` の行を消す PR を出す"
        "（消さないと、この確認が対象のまま残ります）".format(row["event_id"]),
        "3. 先行セールが無ければ、`auto` の行だけ消すか、この issue を閉じる",
    ]


def render_presale_body(row: Dict[str, Any], today: dt.date) -> str:
    lines = [marker_comment(presale_marker_for(row["event_id"])), "", *_presale_lines(row)]
    if row.get("note"):
        lines += ["", "メモ: {}".format(row["note"])]
    lines += ["", "---",
              "`scripts/sale_calendar_reminder.py` による自動起票（{} 判定）。"
              "Refs #9449・navi-brain#206".format(today.isoformat())]
    return "\n".join(lines) + "\n"


def render_presale_comment(row: Dict[str, Any], today: dt.date) -> str:
    """目安で立てた issue が開いているときは、新しく立てずにそこへ足す。"""
    lines = [marker_comment(presale_marker_for(row["event_id"])), "", *_presale_lines(row),
             "", "---",
             "`scripts/sale_calendar_reminder.py` による自動コメント（{} 判定）。".format(
                 today.isoformat())]
    return "\n".join(lines) + "\n"


# --- GitHub 側 (check_delivery_freshness.py と同じ作法) ----------------------

def _gh(args: List[str]) -> str:
    import subprocess
    # encoding を明示する (Windows の既定 cp932 だと日本語の応答で壊れる)
    res = subprocess.run(["gh", *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return res.stdout


def find_existing(repo: str, event_id: str,
                  marker: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """このセールのリマインド issue (open / closed とも) があれば返す。

    marker を渡すと、その印の issue を探す (先行セールの確認の issue)。
    """
    marker = marker or marker_for(event_id)
    query = 'repo:{} is:issue in:body "{}"'.format(repo, marker)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=20"])
    items = json.loads(out).get("items", [])
    # 検索結果は候補でしかない。マーカーコメントの実体で検証してから採用する (#6622)
    matches = verified_matches(items, marker)
    return matches[0] if matches else None


def announcement_posted(repo: str, issue: Dict[str, Any], event_id: str,
                        marker: Optional[str] = None) -> bool:
    """発表の候補 (marker を渡せばその印) を、この issue の本文かコメントに既に載せたか。"""
    needle = marker_comment(marker or announce_marker_for(event_id))
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


def decide(repo: str, row: Dict[str, Any]) -> tuple:
    """この行で何をするか: ("create", None) / ("comment", issue) / ("skip", None)。

    - 目安のリマインド: 同じ印の issue が open / closed とも無ければ立てる
    - 発表の候補: 目安の issue が開いていれば 1 回だけコメント、閉じていれば何もしない
    - 先行セールの確認: 目安の issue が開いていれば 1 回だけコメント。そうでなければ
      確認の印の issue が open / closed とも無いときだけ立てる
    """
    event_id = row["event_id"]
    existing = find_existing(repo, event_id)
    if row.get("presale"):
        marker = presale_marker_for(event_id)
        if existing and existing.get("state") == "open":
            if announcement_posted(repo, existing, event_id, marker):
                logger.info("%s: issue #%s に先行セールの確認を載せ済み", event_id, existing["number"])
                return ("skip", None)
            return ("comment", existing)
        if find_existing(repo, event_id, marker):
            logger.info("%s: 先行セールの確認の issue が既にある", event_id)
            return ("skip", None)
        return ("create", None)
    if not existing:
        return ("create", None)
    if not row.get("candidate"):
        logger.info("%s: issue #%s が既にあるので立てない", event_id, existing["number"])
        return ("skip", None)
    if existing.get("state") != "open":
        logger.info("%s: issue #%s は閉じているので何もしない", event_id, existing["number"])
        return ("skip", None)
    if announcement_posted(repo, existing, event_id):
        logger.info("%s: issue #%s に発表を載せ済み", event_id, existing["number"])
        return ("skip", None)
    return ("comment", existing)


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
    presales = presale_checks(calendar, events, today)
    detected, feed_errors = sale_announcements.collect(
        calendar, registered_ids(events), today, registered_spans(events),
        registered_events(events))
    # 発表が出ているセールを先に (開始が近い順。日付が無ければ発表日)。次に先行セールの
    # 確認、目安だけのセールは最後 (navi-brain#206 R3-1)
    # 関門を通る候補は 62-shop-sale-watch.yml が登録するので、issue にしない
    detected = {k: c for k, c in detected.items() if not sale_announcements.auto_eligible(c)}

    def urgency(c: Dict[str, Any]) -> tuple:
        starts = [p["start"] for p in c["periods"] + c.get("reference_periods", [])]
        return (min(starts) if starts else c["sources"][0]["published"], c["event_id"])
    rows = [{"event_id": c["event_id"], "calendar_id": c["calendar_id"],
             "shop": c.get("shop", "amazon"), "name": c["name"], "year": c["year"], "candidate": c}
            for c in sorted(detected.values(), key=urgency)]
    rows += [r for r in presales if r["event_id"] not in detected]
    rows += [r for r in due if r["event_id"] not in detected]
    logger.info("today=%s detected=%s presale=%s due=%s", today.isoformat(),
                sorted(detected) or "none", [r["event_id"] for r in presales] or "none",
                [r["event_id"] for r in due] or "none")
    if not args.repo and not args.dry_run and rows:
        logger.error("--repo (または環境変数 REPO) が無いと issue を立てられない")
        return 2

    written = False
    for row in rows:
        event_id = row["event_id"]
        if written:
            # 書いた後は検索もしない (GitHub API を連打しない)
            logger.info("%s: 1 run で書くのは 1 件まで。次の run に回す", event_id)
            continue
        action = decide(args.repo, row) if args.repo else ("create", None)
        if action[0] == "skip":
            continue
        kind, existing = action
        presale = bool(row.get("presale"))
        if kind == "comment":
            body = (render_presale_comment if presale else render_comment)(row, today)
            if args.dry_run:
                logger.info("[dry-run] issue #%s へのコメント:\n%s",
                            existing["number"] if existing else "?", body)
            else:
                logger.info("%s: issue #%s にコメントした %s", event_id, existing["number"],
                            create_comment(args.repo, existing["number"], body))
        else:
            title = title_for(row)
            body = (render_presale_body if presale else render_body)(row, today)
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
