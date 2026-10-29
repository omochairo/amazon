#!/usr/bin/env python3
"""楽天・Yahoo の公式告知と Amazon の公式発表からセールの日程を読み、hugo/data/sale_events.yaml に足す (#9449)。

    python scripts/shop_sale_watch.py            # 読んで、足すものを出すだけ (書かない)
    python scripts/shop_sale_watch.py --write    # sale_events.yaml の末尾に足す

60-shop-sale-watch.yml が毎日回し、足したものを PR にして自動マージする。
sale_events.yaml は navi と omcha.jp・home.omcha.jp のセール日程の正本なので、
ここで足した日程は両方のサイトに出る。元は omochairo/omcha-ops の
scripts/shop_sale_watch.py (2026-10-22) で、正本をこちらに寄せたときに移した。

**自動で登録してよいのは、次の関門を全部通ったものだけ** (2026-10-29 K 決定):
  1. 出典が公式のページで、期間を書き方の決まった場所から読んだ
     (楽天は「ポイントアップ期間」の直後、Yahoo は「最終3日間」の直後。
     エントリー期間・事前期間・くじの期間は拾わない)
  2. 開始と終了の日付・時刻が全部読めた
  3. 曜日が暦と合い、期間の長さと何日先かが上限の内
  4. test_sale_events.py が通る (PR の必須チェック)
どれかを外れたら足さない (迷ったら出さない)。

読むのは公式のページだけ (1 run で各 1 回・リリース本文は最大 MAX_RELEASES 本):
  - 楽天市場 お買い物マラソン   https://event.rakuten.co.jp/campaign/point-up/marathon/
  - 楽天スーパーSALE            https://event.rakuten.co.jp/campaign/supersale/
  - LINEヤフーのニュース一覧    https://www.lycorp.co.jp/ja/news/ (題名に「超PayPay祭」のリリースだけ本文を読む)
楽天の robots.txt は /campaign/ を禁じていない。楽天の利用規約が禁じる自動化は
「商品を購入すること (商品ページ上の情報取得等を含む)」で、キャンペーンのページは
商品ページではない (omcha-ops で 2026-10-22 確認、2026-10-29 に robots.txt を再確認)。
商品ページ・検索結果は読まない。
Amazon は sale_announcements.py が読む公式フィードの発表のうち、開始と終了の日時が
題名に全部あるものだけ足す (amazon_events)。Amazon.co.jp 本体は読まない。

決めごと:
  - **人が書いたイベントは書き換えない。** 足すのは、id が無く、同じモールの登録済みの
    期間とも重ならないものだけ。読んだ期間が既にある自動のイベントと食い違えば、
    書き換えずに赤で終わる (公式が日程を変えたのか読み違いかは人が見る)
  - **ページは読めたが期間が無いのは普通** (開催していない時期)。ページそのものが
    読めない・期待する見出しが無いときは赤で終わる (検知が止まったまま緑にしない)
  - **Yahoo の「25時59分」は翌日 1:59 にする**
  - 終わったイベントは消さない (sale_events_live.html がビルド時に落とす)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import logging
import pathlib
import re
import sys
import unicodedata
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sale_announcements  # noqa: E402
from scripts import sale_calendar_reminder as scr  # noqa: E402

logger = logging.getLogger("shop_sale_watch")

SALE_EVENTS = scr.SALE_EVENTS
JST = dt.timezone(dt.timedelta(hours=9))
USER_AGENT = "navi-omcha-sale-calendar/1.0 (+https://navi.omcha.jp/)"
TIMEOUT_S = 30
MAX_BYTES = 3 * 1024 * 1024
WEEKDAYS = "月火水木金土日"
# これより前に出たリリースは見ない
MAX_AGE_DAYS = 60
MAX_RELEASES = 2
# これより長い期間・先の日付は読み違いとみなす
MAX_SPAN_DAYS = 31
MAX_LEAD_DAYS = 120

RAKUTEN_PAGES = (
    {"kind": "marathon", "name": "楽天市場 お買い物マラソン",
     "url": "https://event.rakuten.co.jp/campaign/point-up/marathon/",
     "title": "お買い物マラソン"},
    {"kind": "supersale", "name": "楽天スーパーSALE",
     "url": "https://event.rakuten.co.jp/campaign/supersale/",
     "title": "楽天スーパーSALE"},
)
LYCORP_NEWS = "https://www.lycorp.co.jp/ja/news/"
PAYPAY_NAME = "超PayPay祭"


class SourceError(RuntimeError):
    """ページを取れなかった / 期待する形でなかった。"""


# --- 取得と整形 ---------------------------------------------------------------

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            data = res.read(MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - どの失敗でも「取れなかった」として扱う
        raise SourceError("取得に失敗: {}: {}".format(url, exc)) from exc
    if len(data) > MAX_BYTES:
        raise SourceError("大きすぎる: {}".format(url))
    return data.decode("utf-8", errors="replace")


def page_text(raw: str) -> str:
    """タグを落として 1 行にし、NFKC で全角の括弧・数字・コロンを半角にそろえる。"""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unicodedata.normalize("NFKC", html.unescape(text))
    return re.sub(r"\s+", " ", text)


def page_title(raw: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    return unicodedata.normalize("NFKC", html.unescape(m.group(1))).strip() if m else ""


def _day(y: int, m: int, d: int, w: str) -> Optional[dt.date]:
    """暦にある日付で、曜日が合うときだけ返す。"""
    try:
        day = dt.date(y, m, d)
    except ValueError:
        return None
    return day if WEEKDAYS[day.weekday()] == w else None


def _sane(start: dt.datetime, end: dt.datetime, now: dt.datetime) -> bool:
    return (start < end
            and end - start <= dt.timedelta(days=MAX_SPAN_DAYS)
            and start - now <= dt.timedelta(days=MAX_LEAD_DAYS))


def _event(event_id: str, shop: str, name: str, start: dt.datetime, end: dt.datetime,
           source: str) -> Dict[str, Any]:
    # end は公式の「09:59 まで」= その分の終わりまで
    return {"id": event_id, "shop": shop, "name": name, "source": source,
            "start": start, "end": end + dt.timedelta(seconds=59)}


# --- 楽天 ---------------------------------------------------------------------

# 「ポイントアップ期間 2026年10月24日(土)20:00~2026年10月27日(火)09:59」(NFKC 後)
_RK_DT = r"(\d{4})年(\d{1,2})月(\d{1,2})日\(([月火水木金土日])\)\s*(\d{1,2}):(\d{2})"
RAKUTEN_PERIOD_RE = re.compile(r"ポイントアップ期間\s*" + _RK_DT + r"\s*[~〜\-]\s*" + _RK_DT)


def parse_rakuten(raw: str, page: Dict[str, str], now: dt.datetime) -> List[Dict[str, Any]]:
    """ページから開催期間を読む。見出しが無ければ SourceError、期間が無ければ []。"""
    if page["title"] not in page_title(raw):
        raise SourceError("題名に「{}」が無い: {} (ページが変わった?)".format(
            page["title"], page["url"]))
    m = RAKUTEN_PERIOD_RE.search(page_text(raw))
    if not m:
        return []
    g = m.groups()
    sd = _day(int(g[0]), int(g[1]), int(g[2]), g[3])
    ed = _day(int(g[6]), int(g[7]), int(g[8]), g[9])
    if not sd or not ed or int(g[4]) > 23 or int(g[10]) > 23:
        logger.warning("曜日・時刻が暦と合わないので捨てた: %s", m.group(0))
        return []
    start = dt.datetime.combine(sd, dt.time(int(g[4]), int(g[5])), JST)
    end = dt.datetime.combine(ed, dt.time(int(g[10]), int(g[11])), JST)
    if not _sane(start, end, now):
        logger.warning("期間が不自然なので捨てた: %s", m.group(0))
        return []
    return [_event("rakuten-{}-{}".format(page["kind"], sd.isoformat()), "rakuten",
                   page["name"], start, end, page["url"])]


# --- Yahoo (LINEヤフーのプレスリリース) ---------------------------------------

_NEWS_ITEM_RE = re.compile(
    r'<a href="(?P<url>https://www\.lycorp\.co\.jp/ja/news/release/\d+/)"[^>]*>(?P<body>.*?)</a>',
    re.S)


def parse_news_list(raw: str, now: dt.datetime) -> List[Dict[str, Any]]:
    """題名に「超PayPay祭」があり、MAX_AGE_DAYS 以内に出たリリース。新しい順。
    リリースへのリンクが 1 本も無ければ一覧の形が変わったとみなして SourceError。"""
    items = list(_NEWS_ITEM_RE.finditer(raw))
    if not items:
        raise SourceError("リリースへのリンクが 1 本も無い: {} (ページが変わった?)".format(LYCORP_NEWS))
    out = []
    for m in items:
        body = m.group("body")
        d = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2})"', body)
        p = re.search(r"<p>(.*?)</p>", body, re.S)
        if not d or not p:
            continue
        title = unicodedata.normalize(
            "NFKC", html.unescape(re.sub(r"<[^>]+>", "", p.group(1)))).strip()
        # 「」の中身の完全一致で見る (「超PayPay祭 宝箱くじ」などの関連施策の題名を拾わない)
        if "「{}」".format(PAYPAY_NAME) not in title:
            continue
        published = dt.date.fromisoformat(d.group(1))
        if (now.date() - published).days > MAX_AGE_DAYS:
            continue
        out.append({"url": m.group("url"), "title": title, "published": published})
    out.sort(key=lambda x: x["published"], reverse=True)
    return out


# 「最終3日間:2025年12月13日(土)0時00分~15日(月)25時59分」
# 「実施期間: 2026年7月10日(金)0時00分~12日(日)25時59分(最終3日間)」(NFKC 後)
_LY_START = r"(\d{4})年(\d{1,2})月(\d{1,2})日\(([月火水木金土日])\)\s*(\d{1,2})時(\d{2})分"
_LY_END = r"(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日\(([月火水木金土日])\)\s*(\d{1,2})時(\d{2})分"
_LY_PERIOD = _LY_START + r"\s*[~〜\-]\s*" + _LY_END
YAHOO_FINAL_RE = re.compile(
    r"最終3日間\s*:\s*" + _LY_PERIOD + r"|実施期間\s*:\s*" + _LY_PERIOD + r"\s*\(最終3日間\)")


def parse_paypay_release(raw: str, url: str, now: dt.datetime) -> List[Dict[str, Any]]:
    m = YAHOO_FINAL_RE.search(page_text(raw))
    if not m:
        return []
    g = m.groups()
    g = g[:12] if g[0] else g[12:]
    sy, sm, sd_, sw, sh, smin = int(g[0]), int(g[1]), int(g[2]), g[3], int(g[4]), int(g[5])
    ey = int(g[6]) if g[6] else sy
    em = int(g[7]) if g[7] else sm
    ed_, ew, eh, emin = int(g[8]), g[9], int(g[10]), int(g[11])
    if not g[6] and em < sm:  # 年をまたぐ (12/31〜1/2)
        ey += 1
    sd = _day(sy, sm, sd_, sw)
    ed = _day(ey, em, ed_, ew)
    if not sd or not ed or sh > 23 or eh > 29 or smin > 59 or emin > 59:
        logger.warning("日付・時刻が暦と合わないので捨てた: %s", m.group(0))
        return []
    start = dt.datetime.combine(sd, dt.time(sh, smin), JST)
    # 「25時59分」は翌日 1:59
    end = dt.datetime.combine(ed, dt.time(0, emin), JST) + dt.timedelta(hours=eh)
    if not _sane(start, end, now):
        logger.warning("期間が不自然なので捨てた: %s", m.group(0))
        return []
    return [_event("yahoo-paypay-final-{}".format(sd.isoformat()), "yahoo",
                   "Yahoo!ショッピング {} 最終3日間".format(PAYPAY_NAME), start, end, url)]


# --- まとめ -------------------------------------------------------------------

def collect(now: dt.datetime, get: Callable[[str], str] = fetch
            ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """各出典から読む。1 つ失敗しても他は読む。(見つけたイベント, 失敗の一覧)"""
    found = []  # type: List[Dict[str, Any]]
    errors = []  # type: List[str]
    for page in RAKUTEN_PAGES:
        try:
            found += parse_rakuten(get(page["url"]), page, now)
        except SourceError as exc:
            errors.append(str(exc))
    try:
        for rel in parse_news_list(get(LYCORP_NEWS), now)[:MAX_RELEASES]:
            found += parse_paypay_release(get(rel["url"]), rel["url"], now)
    except SourceError as exc:
        errors.append(str(exc))
    # 終わった回をページが出したままのことがある
    return [f for f in found if f["end"] >= now], errors


def amazon_events(candidates: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """sale_announcements.py の Amazon の候補のうち、関門を通ったものをイベントにする。

    通すのは sale_announcements.auto_eligible の候補だけ (公式の発表だけで日程が 1 つに
    定まり、日時が題名に全部あり、カレンダーにあるセール)。それ以外は今までどおり
    sale_calendar_reminder.py が issue にして人が確かめる。
    先行セールは題名に出ないので、足すのは本番の期間だけ (先行セールの間は帯が出ない側に倒れる)。
    """
    out = []
    for cand in candidates.values():
        if not sale_announcements.auto_eligible(cand):
            continue
        official = [s for s in cand["sources"] if not s.get("news")]
        period = cand["official_periods"][0]
        start = dt.datetime.combine(period["start"], dt.time(*period["start_time"]), JST)
        end = dt.datetime.combine(period["end"], dt.time(*period["end_time"]), JST)
        if not start < end:
            continue
        out.append(_event(cand["event_id"], cand.get("shop", "amazon"), cand["name"],
                          start, end, official[0]["link"]))
    return out


def plan(found: Sequence[Dict[str, Any]], events: Sequence[Dict[str, Any]]
         ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """足すイベントと、食い違い (赤で終わる理由) を返す。"""
    by_id = {e.get("id"): e for e in events if isinstance(e, dict)}
    spans = scr.registered_spans(events)
    add, conflicts = [], []
    for f in found:
        have = by_id.get(f["id"])
        if have is None:
            # 同じ回のリリースが 2 本あると同じ id を 2 回読む。2 本目は 1 本目と突き合わせる
            have = next(({"phases": [{"start": _stamp(a["start"]), "end": _stamp(a["end"])}]}
                         for a in add if a["id"] == f["id"]), None)
        if have is not None:
            phases = have.get("phases") or [{}]
            if (str(phases[0].get("start")), str(phases[-1].get("end"))) != (
                    _stamp(f["start"]), _stamp(f["end"])):
                conflicts.append("{}: 登録済みの期間と読んだ期間が違う ({} 〜 {})".format(
                    f["id"], _stamp(f["start"]), _stamp(f["end"])))
            continue
        day = f["start"].date()
        if any(lo <= day <= hi and shop == f["shop"] for lo, hi, shop in spans):
            # 人が別の id で登録済み
            continue
        add.append(f)
    return add, conflicts


def _stamp(t: dt.datetime) -> str:
    return t.astimezone(JST).isoformat()


def render(f: Dict[str, Any], today: dt.date) -> str:
    """sale_events.yaml の末尾に足す 1 件。値は YAML の文字列として安全に書く。"""
    def q(value: str) -> str:
        return yaml.safe_dump(value, allow_unicode=True, default_style='"').strip()
    return "\n".join([
        "- id: {}".format(f["id"]),
        "  shop: {}".format(f["shop"]),
        "  name: {}".format(q(f["name"])),
        "  source: {}".format(q(f["source"])),
        "  verified_on: {}".format(today.isoformat()),
        "  auto: shop_sale_watch",
        "  phases:",
        "    - label: {}".format(q(f["name"])),
        "      headline: {}".format(q(f["name"])),
        "      start: {}".format(q(_stamp(f["start"]))),
        "      end: {}".format(q(_stamp(f["end"]))),
    ]) + "\n"


def append_events(path: pathlib.Path, add: Sequence[Dict[str, Any]], today: dt.date) -> None:
    # 冒頭のコメント (書き方の説明) を残すため、読み直して書くのではなく末尾に足す
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(render(f, today) for f in add), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="sale_events.yaml に足す")
    ap.add_argument("--now", help="判定の基準時刻 (ISO 8601)。検証用")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(JST)

    found, errors = collect(now)
    events = scr.load_yaml(SALE_EVENTS)
    # Amazon は公式フィードの発表から (フィードが読めないのは reminder 側が赤にする)
    candidates, _ = sale_announcements.collect(
        scr.load_yaml(scr.CALENDAR), scr.registered_ids(events), now.astimezone(JST).date(),
        scr.registered_spans(events))
    found += amazon_events(candidates)
    add, conflicts = plan(found, events)
    for f in found:
        logger.info("見つけた: %s %s 〜 %s", f["id"], _stamp(f["start"]), _stamp(f["end"]))
    for f in add:
        logger.info("足す: %s", f["id"])
    logger.info("見つけた %d 件・足す %d 件・失敗 %d 件・食い違い %d 件",
                len(found), len(add), len(errors), len(conflicts))
    if args.write and add:
        append_events(SALE_EVENTS, add, now.astimezone(JST).date())
    for msg in errors + conflicts:
        logger.error("%s", msg)
    return 1 if errors or conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
