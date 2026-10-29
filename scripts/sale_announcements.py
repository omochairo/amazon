#!/usr/bin/env python3
"""公式のフィードから、セール開催の発表を見つけて日程の「候補」を作る (#9449)。

scripts/sale_calendar_reminder.py が呼ぶ。ここがやるのは候補を作るところまでで、
**サイトに出る日程 (hugo/data/sale_events.yaml) には書かない。** 関門を通った候補だけ
scripts/shop_sale_watch.py が登録し、残りの候補は issue に
載り、人が出典を開いて確かめてから登録する。

読むのは公開のフィード 3 本だけ (1 run で各 1 回):
  - About Amazon Japan (公式ニュースルーム) の RSS
  - PR TIMES のアマゾンジャパンの企業別 RSS
  - PHILE WEB の RSS (報道。公式の 2 本は大型セールしか発表しないので、月例の
    スマイルSALE などはここで拾う。2026-11 のスマイルSALE を取り逃した反省)
Amazon.co.jp 本体は読まない (利用規約がロボットでの収集を認めていない)。

決めごと:
  - **拾うのは題名だけ。** 本文は読まない。先行セールの日程は本文にしか無いので、
    候補には入らない (人が出典で確かめる)
  - **カレンダーのセール名は「」の中身の完全一致で見る。** 同じフィードに、日付の
    違う関連の発表が毎回混ざる (「Amazon プライムデー フェス」を 6/27 から、
    「プライムデー特選セール」を…)。部分一致で拾うと取り違える
  - **カレンダーに無いセールも拾う。** 「」の中身がセールの名前らしく (セール /
    SALE / 祭 などを含む)、既知のセール名を含まないもの。既知の名前を含むものは
    上の関連の発表なので捨てる。報道のフィードでは、Amazon のセールだと題名で
    言い切っているもの (「Amazon「…」」/「「Amazon …」」) だけにする
  - **報道の題名は書き方がそろっていない。** 「11/4 9時スタート」のように終了日の
    無いものや、「11/4(水)〜11/12(木)」の形も読む。終了日が無ければ YAML の終了は
    「??」のままにする (曜日が書いてあれば暦と突き合わせるのは同じ)
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
# どのモールのセールか。hugo/data/sale_events.yaml と data/sale_calendar.yaml の shop
SHOPS = ("amazon", "rakuten", "yahoo")
FEEDS: Tuple[Dict[str, Any], ...] = (
    {"name": "About Amazon Japan",
     "url": "https://www.aboutamazon.jp/rss/feed.rss",
     "shop": "amazon", "hosts": ("www.aboutamazon.jp",)},
    {"name": "PR TIMES（アマゾンジャパン）",
     "url": "https://prtimes.jp/companyrdf.php?company_id=4612",
     "shop": "amazon", "hosts": ("prtimes.jp",)},
    # 報道。Amazon 以外の記事も混ざり、題名の書き方もそろっていない
    {"name": "PHILE WEB", "news": True, "upgrade_http": True,
     "url": "https://www.phileweb.com/rss.php",
     "shop": "amazon", "hosts": ("www.phileweb.com",)},
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
# 「Amazon「スマイルSALE」」の形 (報道で Amazon のセールだと言い切っている題名)
AMAZON_QUOTED_RE = re.compile(r"amazon[\s、,:]*「([^」]{1,40})」", re.IGNORECASE)
# カレンダーに無いセールを拾うときの、セールの名前らしさ (NFKC・小文字にしてから見る)
SALE_WORDS = ("セール", "sale", "祭", "初売り", "フライデー", "サイバーマンデー")


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


def _loose_date(prefix: str, month_required: bool) -> str:
    """報道の題名の日付。「11/4」「11月4日」の両方と、曜日・時刻の省略を許す。"""
    month = r"(?P<{p}m>\d{{1,2}})\s*(?:月|/)\s*"
    if not month_required:
        month = "(?:" + month + ")?"
    return (
        r"(?<!\d)(?:(?P<{p}y>\d{{4}})\s*(?:年|/)\s*)?" + month +
        r"(?P<{p}d>\d{{1,2}})(?!\d)\s*日?\s*(?:\((?P<{p}w>[月火水木金土日])(?:・[祝休])?\))?\s*"
        r"(?:(?P<{p}H>\d{{1,2}})\s*(?:時\s*(?:(?P<{p}M>\d{{1,2}})\s*分)?|:(?P<{p}C>\d{{2}})))?"
    ).format(p=prefix)


LOOSE_RANGE_RE = re.compile(
    _loose_date("s", True) + r"\s*(?:から|[〜~ー-])\s*" + _loose_date("e", False))
LOOSE_START_RE = re.compile(_loose_date("s", True) + r"\s*(?:スタート|開始|から|より)")


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
    # PHILE WEB は http のリンクを配る。同じホストは https でも開けるので上げる
    schemes = ("https", "http") if feed.get("upgrade_http") else ("https",)
    if parts.hostname not in feed["hosts"] or parts.scheme not in schemes:
        return feed["url"]
    parts = parts._replace(scheme="https")
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


def _quoted(title: str, news: bool) -> List[str]:
    """題名の「」の中身。報道では Amazon のものと言い切っている形だけ返す
    (他社の「ブラックフライデー」を Amazon のものと取り違えないため)。"""
    if not news:
        return QUOTED_RE.findall(title)
    text = unicodedata.normalize("NFKC", title)
    # 「Amazon ブラックフライデー」の形。「Amazon対抗セール」(他社) は空白が無いので外れる
    return AMAZON_QUOTED_RE.findall(text) + [
        q for q in QUOTED_RE.findall(text) if re.match(r"\s*amazon\s", q, re.IGNORECASE)]


def match_sale(title: str, calendar: Sequence[Dict[str, Any]],
               news: bool = False) -> Optional[Dict[str, Any]]:
    """題名の「」の中身がカレンダーのセール名と完全一致する 1 件を返す。

    毎月のセール (cycle: monthly) だけは前方一致も見る。スマイルSALE は
    「スマイルSALE ゴールデンウィーク」のような回ごとの呼び名で出ることがあり、
    完全一致だと取り逃す (完全一致の別のセール、例えば新生活が先に当たる)。
    """
    quoted = {normalize_name(q) for q in _quoted(title, news)}
    hits = [e for e in calendar if quoted.intersection(announce_names(e))]
    if not hits:
        hits = [e for e in calendar if e.get("cycle") == "monthly"
                and any(q.startswith(n) for q in quoted for n in announce_names(e))]
    return hits[0] if len(hits) == 1 else None


def match_unknown_sale(title: str, calendar: Sequence[Dict[str, Any]],
                       news: bool = False) -> Optional[str]:
    """カレンダーに無いセールの名前を返す。無ければ None。

    既知のセール名を含む名前 (「プライムデー特選セール」など) は、そのセールの関連の
    発表なので返さない。
    """
    quoted = _quoted(title, news)
    known = [n for e in calendar for n in announce_names(e)]
    names = []
    for raw in quoted:
        name = normalize_name(raw)
        if not name or not any(w in name for w in SALE_WORDS):
            continue
        if any(k in name for k in known):
            continue
        label = re.sub(r"\s+", " ", raw).strip()
        if not re.match(r"amazon", label, re.IGNORECASE):
            label = "Amazon " + label
        if label not in names:
            names.append(label)
    return names[0] if len(names) == 1 else None


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


def _loose_resolve(m: "re.Match", p: str, base: dt.date, month: Optional[int],
                   lo: int, hi: int) -> Optional[dt.date]:
    """_loose_date の一致を日付にする。曜日があれば暦と突き合わせる。"""
    month = int(m.group(p + "m")) if m.group(p + "m") else month
    if month is None:
        return None
    day = int(m.group(p + "d"))
    years = [int(m.group(p + "y"))] if m.group(p + "y") else [base.year, base.year + 1]
    for year in years:
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if m.group(p + "w") and WEEKDAYS[date.weekday()] != m.group(p + "w"):
            continue
        if lo <= (date - base).days <= hi:
            return date
    return None


def _loose_clock(m: "re.Match", p: str) -> Optional[Tuple[int, int]]:
    return _clock(m.group(p + "H"), m.group(p + "M") or m.group(p + "C"))


def parse_loose_period(title: str, published: dt.date) -> Optional[Dict[str, Any]]:
    """報道の題名から開催期間を読む。終了日が無ければ end は None。

    「11/4〜12(木)」のような終了の月が無い形は、終了に曜日か月が書いてあるときだけ
    範囲として読む (「11/4〜12」の 12 が日付か個数か題名だけでは決められない)。
    """
    text = unicodedata.normalize("NFKC", title)
    m = LOOSE_RANGE_RE.search(text)
    if m and not (m.group("em") or m.group("ew")):
        m = None
    if m:
        start = _loose_resolve(m, "s", published, None, 0, MAX_LEAD_DAYS)
        end = start and _loose_resolve(m, "e", start, start.month, 0, MAX_SPAN_DAYS)
        if start and end:
            return {"start": start, "end": end,
                    "start_time": _loose_clock(m, "s"), "end_time": _loose_clock(m, "e")}
        # 範囲として書いてあるのに暦と合わない題名は、開始だけ拾い直さずに捨てる
        return None
    m = LOOSE_START_RE.search(text)
    if not m:
        return None
    start = _loose_resolve(m, "s", published, None, 0, MAX_LEAD_DAYS)
    if start is None:
        return None
    return {"start": start, "end": None,
            "start_time": _loose_clock(m, "s"), "end_time": None}


# --- 候補 -------------------------------------------------------------------

def event_key(entry: Optional[Dict[str, Any]], name: str,
              start: dt.date) -> Tuple[str, Any]:
    """候補のイベント id と、issue の題名に出す回の呼び名。

    毎年のセールは「<id>-<年>」、毎月のセール (cycle: monthly) は「<id>-<年>-<月>」。
    カレンダーに無いセールは名前から id を作れないので、開始日で「adhoc-<開始日>」。
    """
    if entry is None:
        return "adhoc-{}".format(start.isoformat()), start.isoformat()
    if entry.get("cycle") == "monthly":
        return ("{}-{}-{:02d}".format(entry["id"], start.year, start.month),
                "{}年{}月".format(start.year, start.month))
    return "{}-{}".format(entry["id"], start.year), start.year


def shop_of(entry: Dict[str, Any]) -> str:
    """カレンダー・フィードの shop (書いていなければ amazon)。"""
    return entry.get("shop") or "amazon"


def _covered(start: dt.date, shop: str,
             spans: Sequence[Tuple[dt.date, dt.date, str]]) -> bool:
    # 別のモールのセールと期間が重なるのは普通 (楽天のマラソンとスマイルSALE など)
    return any(lo <= start <= hi and s == shop for lo, hi, s in spans)


def _merge_period(periods: List[Dict[str, Any]], period: Dict[str, Any]) -> None:
    # 開始日が同じで、終了日が同じか片方に無いものは同じ日程とみなす
    same = next((p for p in periods
                 if p["start"] == period["start"]
                 and (p["end"] == period["end"] or None in (p["end"], period["end"]))),
                None)
    if same is None:
        periods.append(period)
    else:
        # 書いてある方を採る (終了日・時刻)
        for key in ("end", "start_time", "end_time"):
            same[key] = same[key] or period[key]


def auto_eligible(cand: Dict[str, Any]) -> bool:
    """人の確認なしで sale_events.yaml に登録してよい候補か (2026-10-29 K 決定)。

    公式のフィードの発表だけで日程が 1 つに定まり、開始と終了の日付・時刻が全部
    題名に書いてある、カレンダーにあるセールだけ。カレンダーに無いセール (adhoc) は
    部門別・単発のことがあり、全ページに出してよいかを人が決めるので通さない。
    登録は scripts/shop_sale_watch.py、通らなかったものの issue は
    scripts/sale_calendar_reminder.py。
    """
    if not cand.get("calendar_id") or len(cand.get("official_periods") or []) != 1:
        return False
    period = cand["official_periods"][0]
    return None not in (period["end"], period["start_time"], period["end_time"])


def detect(
    feed_items: Sequence[Tuple[Dict[str, Any], Sequence[Dict[str, Any]]]],
    calendar: Sequence[Dict[str, Any]],
    registered: set,
    today: dt.date,
    registered_spans: Sequence[Tuple[dt.date, dt.date, str]] = (),
) -> Dict[str, Dict[str, Any]]:
    """未登録・これから始まるセールの候補を、イベント id ごとにまとめて返す。

    registered_spans は登録済みのイベントの期間 (JST の日付) とモール。開始日が同じ
    モールの期間に入る候補は、id が違っても登録済みとみなす (カレンダーに無いセールは
    id で突き合わせられないため)。
    """
    found = {}  # type: Dict[str, Dict[str, Any]]
    for feed, items in feed_items:
        news = bool(feed.get("news"))
        shop = shop_of(feed)
        # フィードはモールごと。別のモールのセール名と取り違えない
        shop_calendar = [e for e in calendar if shop_of(e) == shop]
        for item in items:
            age = (today - item["published"]).days
            if not -1 <= age <= MAX_AGE_DAYS:
                continue
            entry = match_sale(item["title"], shop_calendar, news)
            name = entry["name"] if entry else match_unknown_sale(
                item["title"], shop_calendar, news)
            if name is None:
                continue
            period = parse_period(item["title"], item["published"])
            if period is None and news:
                period = parse_loose_period(item["title"], item["published"])
            if period is None:
                continue
            last_day = period["end"] or period["start"] + dt.timedelta(days=MAX_SPAN_DAYS)
            if last_day < today:
                continue
            event_id, label = event_key(entry, name, period["start"])
            if event_id in registered or _covered(period["start"], shop, registered_spans):
                continue
            cand = found.setdefault(event_id, {
                "event_id": event_id, "calendar_id": entry["id"] if entry else None,
                "shop": shop,
                "name": name, "year": label,
                "periods": [], "official_periods": [], "sources": [],
            })
            cand["sources"].append({
                "feed": feed["name"], "news": news, "title": item["title"],
                "link": safe_link(item["link"], feed), "published": item["published"],
            })
            _merge_period(cand["periods"], period)
            if not news:
                # 自動登録の判定 (auto_eligible) は公式の発表だけで見る。報道の時刻で補わない
                _merge_period(cand["official_periods"], dict(period))
    for cand in found.values():
        cand["sources"].sort(key=lambda s: (s["published"], s["link"]))
        for key in ("periods", "official_periods"):
            cand[key].sort(key=lambda p: (p["start"], p["end"] or dt.date.max))
    # 同じ開始日にカレンダーのセールの候補があれば、名前の違う adhoc はその別名とみなす
    # (「スマイルセール」と「スマイルSALE」で 2 本立てない)
    known_starts = {(c["shop"], p["start"])
                    for c in found.values() if c["calendar_id"] for p in c["periods"]}
    for event_id in [k for k, c in found.items() if c["calendar_id"] is None]:
        cand = found[event_id]
        if any((cand["shop"], p["start"]) in known_starts for p in cand["periods"]):
            del found[event_id]
    return found


def collect(
    calendar: Sequence[Dict[str, Any]], registered: set, today: dt.date,
    registered_spans: Sequence[Tuple[dt.date, dt.date, str]] = (),
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
    return detect(feed_items, calendar, registered, today, registered_spans), errors


# --- issue に載せる文面 -----------------------------------------------------

def _stamp(date: Optional[dt.date], clock: Optional[Tuple[int, int]], is_end: bool) -> str:
    if date is None:
        # 終了日が題名に無かった (報道の「11/4 9時スタート」など)
        return "????-??-??T??:??:??+09:00"
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
        "  shop: {}".format(cand.get("shop", "amazon")),
        "  name: {}".format(cand["name"]),
        # 報道は出典にしない (Amazon.co.jp の告知で確かめて書き直してもらう)
        "  source: {}".format("{} の記事 → Amazon.co.jp の告知で確認".format(source["feed"])
                               if source.get("news") else "{} の発表".format(source["feed"])),
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
