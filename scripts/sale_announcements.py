#!/usr/bin/env python3
"""公式のフィードから、セール開催の発表を見つけて日程の「候補」を作る (#9449)。

scripts/sale_calendar_reminder.py が呼ぶ。ここがやるのは候補を作るところまでで、
**サイトに出る日程 (hugo/data/sale_events.yaml) には一切書かない。** 候補は issue に
載り、人が出典を開いて確かめてから登録する。

読むのは公開のフィード 2 本だけ (1 run で各 1 回):
  - About Amazon Japan (公式ニュースルーム) の RSS
  - PR TIMES のアマゾンジャパンの企業別 RSS
Amazon.co.jp 本体は読まない (利用規約がロボットでの収集を認めていない)。

決めごと:
  - **拾うのは題名だけ。** 本文は読まない。先行セールの日程は本文にしか無いので、
    候補には入らない (人が出典で確かめる)
  - **セール名は「」の中身の完全一致で見る。** 同じフィードに、日付の違う関連の
    発表が毎回混ざる (「Amazon プライムデー フェス」を 6/27 から、「プライムデー
    特選セール」を…)。部分一致で拾うと取り違える。名前が変わった年は拾えないが、
    その場合は例年の目安によるリマインドが残る (黙る方向に倒す)
  - **曜日が合わない日付は捨てる。** 題名の「7月10日（金）」の曜日を実際の暦と
    突き合わせる。年の推定や読み取りを誤ったときに、ここで落ちる
  - **題名に無い時刻は埋めない。** 貼り付け用の YAML には「??」を入れ、そのまま
    貼るとテスト (test_sale_events.py) で落ちるようにする (初売りは 9 時開始の
    年があり、0 時と決め打ちできない)
"""
from __future__ import annotations

import datetime as dt
import email.utils
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("sale_announcements")

JST = dt.timezone(dt.timedelta(hours=9))
FEEDS: Tuple[Dict[str, Any], ...] = (
    {"name": "About Amazon Japan",
     "url": "https://www.aboutamazon.jp/rss/feed.rss",
     "hosts": ("www.aboutamazon.jp",)},
    {"name": "PR TIMES（アマゾンジャパン）",
     "url": "https://prtimes.jp/companyrdf.php?company_id=4612",
     "hosts": ("prtimes.jp",)},
)
USER_AGENT = "navi-omcha-sale-calendar/1.0 (+https://navi.omcha.jp/)"
TIMEOUT_S = 20
MAX_FEED_BYTES = 2 * 1024 * 1024
# これより前に出た発表は見ない (PR TIMES 側は 2 年分ほど入っている)
MAX_AGE_DAYS = 60
# 発表から開始までは実績で 7〜31 日。これを超える先の日付は読み違いとみなす
MAX_LEAD_DAYS = 90
MAX_SPAN_DAYS = 14
MAX_TITLE_LEN = 200
WEEKDAYS = "月火水木金土日"

QUOTED_RE = re.compile("「([^」]{1,40})」")


def _date_pattern(prefix: str, month_required: bool) -> str:
    month = r"(?P<{p}m>\d{{1,2}})\s*月\s*"
    if not month_required:
        month = "(?:" + month + ")?"
    return (
        r"(?:(?P<{p}y>\d{{4}})\s*年\s*)?" + month +
        r"(?P<{p}d>\d{{1,2}})\s*日\s*\((?P<{p}w>[月火水木金土日])(?:・[祝休])?\)\s*"
        r"(?:(?P<{p}H>\d{{1,2}})\s*時\s*(?:(?P<{p}M>\d{{1,2}})\s*分)?)?"
    ).format(p=prefix)


# 題名は NFKC に正規化してから当てる (全角の括弧・数字を半角にそろえる)
PERIOD_RE = re.compile(
    _date_pattern("s", True) + r"\s*から\s*(?:" + _date_pattern("e", False) +
    r"|(?P<n>\d{1,2})\s*日間)")


class FeedError(RuntimeError):
    """フィードを取れなかった / 読めなかった。"""


# --- フィード ---------------------------------------------------------------

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            data = res.read(MAX_FEED_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - どの失敗でも「取れなかった」として扱う
        raise FeedError("取得に失敗: {}".format(exc)) from exc
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("フィードが大きすぎる (> {} bytes)".format(MAX_FEED_BYTES))
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_published(raw: str) -> Optional[dt.date]:
    """pubDate (RFC 822) / dc:date (ISO 8601) を JST の日付にする。"""
    raw = raw.strip()
    if not raw:
        return None
    parsed = None  # type: Optional[dt.datetime]
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(JST).date()


def parse_feed(data: bytes) -> List[Dict[str, Any]]:
    """RSS 2.0 / RSS 1.0 (RDF) の item を {title, link, published} にする。"""
    # 実体宣言を持つフィードは読まない (どちらのフィードにも無い。展開攻撃を避ける)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise FeedError("DOCTYPE / ENTITY を含むフィードは読まない")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError("XML として読めない: {}".format(exc)) from exc
    items = []
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in el}
        published = _parse_published(fields.get("pubDate") or fields.get("date") or "")
        if not fields.get("title") or published is None:
            continue
        items.append({"title": fields["title"], "link": fields.get("link", ""),
                      "published": published})
    if not items:
        # 形式が変わって 1 件も読めないのを「発表なし」と区別する
        raise FeedError("item が 1 件も読めない")
    return items


def safe_link(link: str, feed: Dict[str, Any]) -> str:
    """フィードのホストの https リンクだけ通す。それ以外はフィード自体の URL に倒す。"""
    try:
        parts = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return feed["url"]
    if parts.scheme != "https" or parts.hostname not in feed["hosts"]:
        return feed["url"]
    # 計測用のクエリ (?utm_source=rss) は落とす
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# --- 題名の判定 -------------------------------------------------------------

def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    text = re.sub(r"\s+", "", text).lower()
    return text[len("amazon"):] if text.startswith("amazon") else text


def announce_names(entry: Dict[str, Any]) -> List[str]:
    names = [entry["name"]] + list(entry.get("announce_names") or [])
    return [normalize_name(n) for n in names]


def match_sale(title: str, calendar: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """題名の「」の中身がカレンダーのセール名と完全一致する 1 件を返す。"""
    quoted = {normalize_name(q) for q in QUOTED_RE.findall(title)}
    hits = [e for e in calendar if quoted.intersection(announce_names(e))]
    return hits[0] if len(hits) == 1 else None


def _checked_date(year: int, month: int, day: int, weekday: str) -> Optional[dt.date]:
    try:
        date = dt.date(year, month, day)
    except ValueError:
        return None
    return date if WEEKDAYS[date.weekday()] == weekday else None


def _clock(hour: Optional[str], minute: Optional[str]) -> Optional[Tuple[int, int]]:
    if hour is None:
        return None
    h, m = int(hour), int(minute or 0)
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def parse_period(title: str, published: dt.date) -> Optional[Dict[str, Any]]:
    """題名から開催期間を読む。読めない・暦と合わないものは None。"""
    text = unicodedata.normalize("NFKC", title)
    if "開催" not in text:
        return None
    m = PERIOD_RE.search(text)
    if not m:
        return None
    month, day = int(m.group("sm")), int(m.group("sd"))
    years = ([int(m.group("sy"))] if m.group("sy")
             else [published.year, published.year + 1])
    start = None
    for year in years:
        date = _checked_date(year, month, day, m.group("sw"))
        if date and 0 <= (date - published).days <= MAX_LEAD_DAYS:
            start = date
            break
    if start is None:
        return None

    if m.group("n"):
        days = int(m.group("n"))
        if not 1 <= days <= MAX_SPAN_DAYS + 1:
            return None
        end = start + dt.timedelta(days=days - 1)
        end_time = None
    else:
        end_month = int(m.group("em") or month)
        end = None
        for year in (start.year, start.year + 1):
            date = _checked_date(year, end_month, int(m.group("ed")), m.group("ew"))
            if date and 0 <= (date - start).days <= MAX_SPAN_DAYS:
                end = date
                break
        if end is None:
            return None
        end_time = _clock(m.group("eH"), m.group("eM"))
    return {"start": start, "end": end,
            "start_time": _clock(m.group("sH"), m.group("sM")), "end_time": end_time}


# --- 候補 -------------------------------------------------------------------

def detect(
    feed_items: Sequence[Tuple[Dict[str, Any], Sequence[Dict[str, Any]]]],
    calendar: Sequence[Dict[str, Any]],
    registered: set,
    today: dt.date,
) -> Dict[str, Dict[str, Any]]:
    """未登録・これから始まるセールの候補を、イベント id ごとにまとめて返す。"""
    found = {}  # type: Dict[str, Dict[str, Any]]
    for feed, items in feed_items:
        for item in items:
            age = (today - item["published"]).days
            if not -1 <= age <= MAX_AGE_DAYS:
                continue
            entry = match_sale(item["title"], calendar)
            period = parse_period(item["title"], item["published"]) if entry else None
            if not period or period["end"] < today:
                continue
            event_id = "{}-{}".format(entry["id"], period["start"].year)
            if event_id in registered:
                continue
            cand = found.setdefault(event_id, {
                "event_id": event_id, "calendar_id": entry["id"],
                "name": entry["name"], "year": period["start"].year,
                "periods": [], "sources": [],
            })
            cand["sources"].append({
                "feed": feed["name"], "title": item["title"],
                "link": safe_link(item["link"], feed), "published": item["published"],
            })
            same = next((p for p in cand["periods"]
                         if (p["start"], p["end"]) == (period["start"], period["end"])), None)
            if same is None:
                cand["periods"].append(period)
            else:
                # 同じ日付なら、時刻は書いてある方を採る
                for key in ("start_time", "end_time"):
                    same[key] = same[key] or period[key]
    for cand in found.values():
        cand["sources"].sort(key=lambda s: (s["published"], s["link"]))
        cand["periods"].sort(key=lambda p: (p["start"], p["end"]))
    return found


def collect(
    calendar: Sequence[Dict[str, Any]], registered: set, today: dt.date,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """フィードを読んで候補を返す。読めなかったフィードは 2 つ目の戻り値に積む。"""
    feed_items, errors = [], []
    for feed in FEEDS:
        try:
            items = parse_feed(fetch(feed["url"]))
        except FeedError as exc:
            errors.append("{}: {}".format(feed["name"], exc))
            continue
        logger.info("feed %s: %d items", feed["name"], len(items))
        feed_items.append((feed, items))
    return detect(feed_items, calendar, registered, today), errors


# --- issue に載せる文面 -----------------------------------------------------

def _stamp(date: dt.date, clock: Optional[Tuple[int, int]], is_end: bool) -> str:
    if clock is None:
        hhmmss = "??:??:??"
    elif is_end and clock == (23, 59):
        hhmmss = "23:59:59"
    else:
        hhmmss = "{:02d}:{:02d}:00".format(*clock)
    return "{}T{}+09:00".format(date.isoformat(), hhmmss)


def _inline(text: str) -> str:
    """フィード由来の文字列を、issue の中で何も起こさない形 (コードスパン) にする。"""
    text = re.sub(r"\s+", " ", text.replace("`", "'")).strip()
    if len(text) > MAX_TITLE_LEN:
        text = text[:MAX_TITLE_LEN] + "…"
    return "`{}`".format(text)


def render_yaml(cand: Dict[str, Any], period: Dict[str, Any]) -> str:
    source = cand["sources"][0]
    return "\n".join([
        "- id: {}".format(cand["event_id"]),
        "  name: {}".format(cand["name"]),
        "  source: {} の発表".format(source["feed"]),
        "  verified_on: ????-??-??",
        "  phases:",
        "    - label: 本番",
        "      headline: {}".format(cand["name"]),
        '      start: "{}"'.format(_stamp(period["start"], period["start_time"], False)),
        '      end: "{}"'.format(_stamp(period["end"], period["end_time"], True)),
    ])


def render_candidate(cand: Dict[str, Any]) -> List[str]:
    """候補を issue の本文 / コメントに載せる行にする。"""
    lines = ["## 自動で見つけた発表（未確認）", ""]
    for source in cand["sources"][:5]:
        lines.append("- {} {} {} <{}>".format(
            source["published"].isoformat(), source["feed"],
            _inline(source["title"]), source["link"]))
    lines += [
        "",
        "題名から機械的に読んだ日程です。**出典を開いて確かめるまで、正しいとは"
        "限りません。**",
    ]
    if len(cand["periods"]) > 1:
        lines += ["", "**発表によって日程が食い違っています。** どれが正しいかを"
                      "出典で確かめてください。"]
    for period in cand["periods"]:
        lines += ["", "```yaml", render_yaml(cand, period), "```"]
    lines += [
        "",
        "貼る前に、出典で次を確かめて直してください。",
        "",
        "- 開始と終了の日付・時刻（`??` は題名に書かれていなかった箇所。"
        "0 時開始とは限りません）",
        "- 先行セールの有無と期間（題名には出ません。あれば `phases` の先頭に足す）",
        "- `verified_on` に確かめた日を入れる（`??` のままだとテストで落ちます）",
    ]
    return lines
