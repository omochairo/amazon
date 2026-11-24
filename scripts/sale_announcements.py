#!/usr/bin/env python3
"""公式のフィードから、セール開催の発表を見つけて日程の「候補」を作る (#9449)。

scripts/sale_calendar_reminder.py が呼ぶ。ここがやるのは候補を作るところまでで、
**サイトに出る日程 (hugo/data/sale_events.yaml) には書かない。** 関門を通った候補だけ
scripts/shop_sale_watch.py が登録し、残りの候補は issue に
載り、人が出典を開いて確かめてから登録する。

読むのは公開のフィード 5 本だけ (1 run で各 1 回):
  - About Amazon Japan (公式ニュースルーム) の RSS
  - PR TIMES のアマゾンジャパンの企業別 RSS
  - PHILE WEB の RSS (報道。公式の 2 本は大型セールしか発表しないので、月例の
    スマイルSALE などはここで拾う。2026-11 のスマイルSALE を取り逃した反省)
  - PR TIMES の楽天グループ・LINEヤフーの企業別 RSS (楽天市場・Yahoo!ショッピングの
    大型セールの発表。2026-11 の両モールのブラックフライデーを取り逃した反省。navi-brain#206)
Amazon.co.jp 本体は読まない (利用規約がロボットでの収集を認めていない)。

楽天・Yahoo のフィード (mall) の決めごと (navi-brain#206 の決定 1):
  - **セールの名前を決め打ちしない。** モールの名前・開催の語・セールの語を全部含む題名を
    拾う (mall_sale_name)。誤検知 (年に 2 件ほど) は人が issue を閉じて捨てる
  - **日付が無くても知らせる。** 楽天の題名にはほぼ日付が無い。題名の日付は参考
    (reference_periods) にだけ持ち、自動登録の関門 (auto_eligible) には触れない
  - id はカレンダーのセールなら「<id>-<年>」、無ければ「adhoc-<shop>-<発表年>-<発表月>-<名前の
    ハッシュ>」(同じセールの第 2 報で立て直さず、同じ日の別のセールを潰さない)
  - **登録済みとみなすのは慎重に。** 名前は核の語 (SALE_CORES) の共有か完全一致で見て、
    時期も合うときだけ (registered_by_name)。迷ったら知らせる側に倒す

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
import hashlib
import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("sale_announcements")

JST = dt.timezone(dt.timedelta(hours=9))
# どのモールのセールか。hugo/data/sale_events.yaml と data/sale_calendar.yaml の shop
SHOPS = ("amazon", "rakuten", "yahoo")
# 帯の文言を押したときの既定の行き先 (sale_events.yaml の url)。アフィリエイトは付けない
# (hugo/layouts/partials/sale_event_url.html が付ける)。Amazon はおもちゃのセール品の絞り込みで、
# 平常時は「本日のタイムセール」、大型セールの間はそのセールの絞り込みになる
# (omcha-ops で 2026-10-22 に実物を確認)。Amazon.co.jp 本体は読まないので、ここは人が確かめた値
SALE_URLS = {
    "amazon": "https://www.amazon.co.jp/s?rh=n%3A13299531%2Cp_n_deal_type%3A10343614051",
    "yahoo": "https://shopping.yahoo.co.jp/promotion/sale/ppf/",
    # 楽天はセールごとにページが違う。カレンダーに url があればそちらを使う
    "rakuten": "https://event.rakuten.co.jp/",
}
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
    # 楽天・Yahoo の公式の発表 (navi-brain#206)。どちらも企業全体のフィードで、別のサービスの
    # 発表が混ざる。セールの名前は決め打ちせず、モールの名前・開催の語・セールの語で拾う (mall)
    {"name": "PR TIMES（楽天グループ）", "mall": True,
     "url": "https://prtimes.jp/companyrdf.php?company_id=5889",
     "shop": "rakuten", "hosts": ("prtimes.jp",)},
    {"name": "PR TIMES（LINEヤフー）", "mall": True,
     "url": "https://prtimes.jp/companyrdf.php?company_id=129774",
     "shop": "yahoo", "hosts": ("prtimes.jp",)},
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

# --- 楽天・Yahoo のフィード (mall) の決めごと (navi-brain#206) ---
# そのモールの発表とみなす名前 (NFKC・小文字・空白を除いた題名に含まれるか)。
# 「【Yahoo!トラベル】」「「楽天ふるさと納税」」などの別のサービスはこれで落ちる
MALL_NAMES = {"rakuten": ("楽天市場", "楽天スーパーsale"),
              "yahoo": ("yahoo!ショッピング", "ヤフーショッピング", "ヤフショ")}
# 開催の語。「開始」は「提供開始」が多いので入れない
OPEN_WORDS = ("開催", "実施", "スタート")
# 発表からこの日数までだけ候補にする (題名に日付が無いことが多く、終わったかを判定できない。
# 初めて動かしたときに 2 か月前の終わったセールまで issue にしない)
MALL_MAX_AGE_DAYS = 14
# issue の題名で名前の前に付けるモールの名前
MALL_SITES = {"amazon": "Amazon", "rakuten": "楽天市場", "yahoo": "Yahoo!ショッピング"}
# 題名のどこかからセールの名前を切り出す (楽天は名前を「」で囲まない)。助詞 (ひらがな) で切る
SALE_PHRASE_RE = re.compile(
    r"[ァ-ヴーA-Za-z0-9一-龠々!・&+]{0,20}(?:セール|sale|祭|フライデー|初売り|サイバーマンデー)"
    r"[ァ-ヴーA-Za-z0-9]*", re.IGNORECASE)
TODAY_START_RE = re.compile(r"本日(?:から|より)")
# 日付の無い Amazon の公式の題名を候補にするときの開催の書き方。
# 「「プライムデー」の開催に向けた…調査を実施」を拾わないため「を…開催」だけにする
DATELESS_OPEN_RE = re.compile(r"を(?:本日(?:から|より))?開催")
# 同じセールかを名前で見るとき (sale_key) に落とすモールの名前。長いものから
MALL_WORDS = ("amazon.co.jp", "amazon", "アマゾン", "楽天市場", "rakuten", "楽天",
              "yahoo!ショッピング", "yahooショッピング", "ヤフーショッピング", "ヤフショ",
              "yahoo!", "yahoo")
# 「同じセール」とみなす核の語 (sale_key の後の形)。**登録済みとみなす側にだけ使う。**
# ここに無い新しいセールは一致せず、知らせる側に倒れる
SALE_CORES = ("ブラックフライデー", "blackfriday", "サイバーマンデー", "スーパーsale",
              "スーパーセール", "大感謝祭", "初売り", "超paypay祭", "爆買ウルトラ", "マラソン",
              "プライムデー", "プライム感謝祭", "新生活", "スマイルsale", "ビッグボーナス")
CORE_ALIASES = {"blackfriday": "ブラックフライデー", "スーパーセール": "スーパーsale"}
# 名前が同じでも、登録済みのイベントがこの範囲に無ければ別の回とみなす
NAME_MATCH_LEAD_DAYS = 45
NAME_MATCH_SLACK_DAYS = 3
# カレンダーのセールの何年の回か: 目安の日が発表日のこの範囲に来る年
CAL_YEAR_BEFORE_DAYS = 28
CAL_YEAR_AFTER_DAYS = 90


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


def sale_key(name: str) -> str:
    """同じセールかを見るための名前の形。モールの名前・空白・記号を落とす。"""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).lower()
    for word in MALL_WORDS:
        text = text.replace(word, "")
    return re.sub(r"[\W_]", "", text)


def _cores(key: str) -> set:
    return {CORE_ALIASES.get(c, c) for c in SALE_CORES if c in key}


def clearly_different(a: str, b: str) -> bool:
    """名前から、別のセールだと言い切れるか (両方に核の語があり、共有しない)。

    開始日の近さで「人が別の id で書いた同じ回」とみなすとき (_covered・62 の plan) の歯止め。
    名前が無い・核の語が無いときは言い切れない (同じ回とみなす側に倒す)。
    """
    ca, cb = _cores(sale_key(a)), _cores(sale_key(b))
    return bool(ca and cb and not ca & cb)


def name_digest(name: str) -> str:
    """カレンダーに無いセールの id に使う、名前の表記の揺れに左右されないハッシュ 6 桁。

    核の語があればその集合、無ければ末尾の「セール」「sale」を落とした形から作る
    (「ブラックフライデー」と「ブラックフライデーセール」で別の id にしない)。
    """
    key = sale_key(name)
    cores = sorted(_cores(key))
    if cores:
        basis = "|".join(cores)
    else:
        basis = key
        for word in ("セール", "sale"):
            if basis.endswith(word) and len(basis) > len(word):
                basis = basis[:-len(word)]
                break
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:6]


def same_sale(a: str, b: str) -> bool:
    """2 つの名前が同じセールか。核の語があれば共有するか、無ければ完全一致で見る。

    包含では見ない (「スーパー」「感謝祭」のような短い語で別のセールに吸われ、取り逃しが
    再発するため)。空・2 字以下は一致にしない。
    """
    ka, kb = sale_key(a), sale_key(b)
    ca, cb = _cores(ka), _cores(kb)
    if ca or cb:
        return bool(ca & cb)

    def bare(key: str) -> str:
        for word in ("セール", "sale"):
            if key.endswith(word) and len(key) - len(word) >= 3:
                return key[:-len(word)]
        return key
    ka, kb = bare(ka), bare(kb)
    return len(ka) > 2 and ka == kb


def mall_sale_name(title: str, shop: str) -> Optional[str]:
    """楽天・Yahoo のフィードの題名が大型セールの開催の発表らしければ、セールの名前を返す。

    名前は決め打ちしない (navi-brain#206 の決定 1)。条件は、モールの名前・開催の語・
    セールの語を全部含むこと。誤検知は年に数件で、人が issue を閉じて捨てる。
    """
    text = unicodedata.normalize("NFKC", title)
    flat = re.sub(r"\s+", "", text).lower()
    if not any(m in flat for m in MALL_NAMES.get(shop, ())):
        return None
    if not any(w in flat for w in OPEN_WORDS) or not any(w in flat for w in SALE_WORDS):
        return None
    for quoted in QUOTED_RE.findall(text):
        if any(w in normalize_name(quoted) for w in SALE_WORDS):
            return re.sub(r"\s+", " ", quoted).strip()
    m = SALE_PHRASE_RE.search(text)
    if m:
        return m.group(0)
    return re.sub(r"\s+", " ", text).strip()[:40]


def _typical(entry: Dict[str, Any], year: int) -> Optional[dt.date]:
    m = re.match(r"^(\d{2})-(\d{2})$", str(entry.get("typical_start") or ""))
    if not m:
        return None
    try:
        return dt.date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def calendar_match(name: str, calendar: Sequence[Dict[str, Any]],
                   published: dt.date) -> Optional[Tuple[Dict[str, Any], int]]:
    """名前が同じで、目安の日が発表日に最も近い (カレンダーの項目, 年)。毎月のセールは見ない。

    初売りは前年の 12 月に発表されるので、年は発表日の年に決め打ちしない。
    """
    best = None  # type: Optional[Tuple[int, Dict[str, Any], int]]
    for entry in calendar:
        if entry.get("cycle") == "monthly":
            continue
        names = [entry["name"]] + list(entry.get("announce_names") or [])
        if not any(same_sale(name, n) for n in names):
            continue
        for year in (published.year - 1, published.year, published.year + 1):
            day = _typical(entry, year)
            if day is None:
                continue
            diff = (day - published).days
            if -CAL_YEAR_BEFORE_DAYS <= diff <= CAL_YEAR_AFTER_DAYS and (
                    best is None or abs(diff) < best[0]):
                best = (abs(diff), entry, year)
    return (best[1], best[2]) if best else None


def registered_by_name(name: str, shop: str, published: dt.date,
                       reference: Optional[Dict[str, Any]],
                       events: Sequence[Dict[str, Any]]) -> bool:
    """同じモールに、名前が同じで時期の合う登録済みのイベントがあるか。

    events は sale_calendar_reminder.registered_events の形 ({shop, name, lo, hi, ...})。
    時期は「終了 ≧ 発表日」かつ「開始 ≦ 発表日 + NAME_MATCH_LEAD_DAYS」。題名に日付が
    あれば、それが期間 (前後 NAME_MATCH_SLACK_DAYS 日) に入ることも見る (6 月の超PayPay祭の
    登録で、6 月中に出た 7 月の回の発表を黙らせない)。
    """
    slack = dt.timedelta(days=NAME_MATCH_SLACK_DAYS)
    for ev in events:
        if ev.get("shop") != shop or not ev.get("name") or not same_sale(name, ev["name"]):
            continue
        if ev["hi"] < published or ev["lo"] > published + dt.timedelta(days=NAME_MATCH_LEAD_DAYS):
            continue
        # 「本日から開催」から作った参考の日付は事前期間の始まりのことがあり、62 が登録する
        # 「最終3日間」とずれる (超PayPay祭)。時期の突き合わせには使わない
        ref_start = reference and not reference.get("from_today") and reference.get("start")
        if ref_start and not ev["lo"] - slack <= ref_start <= ev["hi"] + slack:
            continue
        return True
    return False


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


def _covered(start: dt.date, shop: str, events: Sequence[Dict[str, Any]],
             name: str = "") -> bool:
    """同じモールに、どれかの段階の開始日が start の前後 1 日にあるイベントがあるか。

    名前から別のセールだと言い切れるもの (clearly_different) は数えない。

    「期間の中に開始日が入る」では見ない。期間が重なる別のセール (楽天のブラックフライデーの
    最中のマラソンなど) を黙って捨てるため (navi-brain#206 の課題 ⑥)。
    別のモールのセールと期間が重なるのは普通 (楽天のマラソンとスマイルSALE など)。
    """
    one = dt.timedelta(days=1)
    return any(ev.get("shop") == shop and any(abs(s - start) <= one for s in ev["starts"])
               and not clearly_different(name, ev.get("name") or "")
               for ev in events)


def _span_events(spans: Sequence[Tuple[dt.date, dt.date, str]]) -> List[Dict[str, Any]]:
    """古い呼び方 (期間とモールだけ) を registered_events の形にする。開始日は期間の初日。"""
    return [{"shop": s, "name": "", "lo": lo, "hi": hi, "starts": [lo]} for lo, hi, s in spans]


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


def mall_event_id(entry: Optional[Dict[str, Any]], year: Optional[int], shop: str,
                  name: str, published: dt.date) -> Tuple[str, Any]:
    """楽天・Yahoo の発表の候補の id と、issue の題名に出す回の呼び名。

    カレンダーのセールなら「<id>-<年>」(目安のリマインドと同じ id になり、issue が 1 本に
    まとまる)。無ければ「adhoc-<shop>-<発表年>-<発表月>-<名前のハッシュ 6 桁>」。
    発表日では作らない: 同じセールの第 2 報で issue が立ち直り、同じ日の別のセールが 1 本に
    潰れるため (2026-11-23 の要件レビュー)。
    """
    if entry is not None:
        return "{}-{}".format(entry["id"], year), year
    digest = name_digest(name)
    return ("adhoc-{}-{}-{:02d}-{}".format(shop, published.year, published.month, digest),
            "{}年{}月".format(published.year, published.month))


def _detect_mall(item: Dict[str, Any], feed: Dict[str, Any], shop: str,
                 shop_calendar: Sequence[Dict[str, Any]], registered: set,
                 events: Sequence[Dict[str, Any]], add: Callable[..., Dict[str, Any]]) -> None:
    """楽天・Yahoo のフィードの 1 件を候補にする (navi-brain#206 の決定 1)。

    題名の日付は「参考」(reference_periods) にだけ入れる。Yahoo の題名の日付は山場の日付の
    ことがあり、楽天はほぼ日付が無い。periods / official_periods には入れないので、
    自動登録の関門 (auto_eligible) は通らない (自動登録は今の関門のまま)。
    """
    name = mall_sale_name(item["title"], shop)
    if name is None:
        return
    published = item["published"]
    reference = parse_period(item["title"], published) or parse_loose_period(
        item["title"], published)
    if reference is None and TODAY_START_RE.search(unicodedata.normalize("NFKC", item["title"])):
        reference = {"start": published, "end": None, "start_time": None, "end_time": None,
                     "from_today": True}
    hit = calendar_match(name, shop_calendar, published)
    entry, year = hit if hit else (None, None)
    event_id, label = mall_event_id(entry, year, shop, name, published)
    if event_id in registered or registered_by_name(
            entry["name"] if entry else name, shop, published, reference, events):
        # 黙って捨てない (navi-brain#206 ⑥): どの発表を登録済みとして読み飛ばしたかを残す
        logger.info("登録済みとして読み飛ばす: %s (%s)", event_id, item["title"])
        return
    display = entry["name"] if entry else "{} {}".format(MALL_SITES[shop], name)
    cand = add(event_id, entry, shop, display, label, feed, item)
    if reference:
        _merge_period(cand["reference_periods"], reference)


def detect(
    feed_items: Sequence[Tuple[Dict[str, Any], Sequence[Dict[str, Any]]]],
    calendar: Sequence[Dict[str, Any]],
    registered: set,
    today: dt.date,
    registered_spans: Sequence[Tuple[dt.date, dt.date, str]] = (),
    registered_events: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """未登録・これから始まるセールの候補を、イベント id ごとにまとめて返す。

    registered_events は登録済みのイベント (sale_calendar_reminder.registered_events の形:
    モール・名前・期間・段階の開始日)。id が違っても、同じモールで段階の開始日が前後 1 日に
    あるか (_covered)、名前と時期が合えば (registered_by_name) 登録済みとみなす
    (カレンダーに無いセールや、62 が別の id で自動登録したものは id で突き合わせられないため)。
    registered_spans は古い呼び方 (期間とモールだけ)。registered_events があればそちらを使う。
    """
    events = (list(registered_events) if registered_events is not None
              else _span_events(registered_spans))
    found = {}  # type: Dict[str, Dict[str, Any]]

    def add(event_id: str, entry: Optional[Dict[str, Any]], shop: str, name: str,
            label: Any, feed: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        cand = found.setdefault(event_id, {
            "event_id": event_id, "calendar_id": entry["id"] if entry else None,
            "shop": shop, "url": (entry or {}).get("url"),
            "name": name, "year": label,
            "periods": [], "official_periods": [], "reference_periods": [], "sources": [],
        })
        cand["sources"].append({
            "feed": feed["name"], "news": bool(feed.get("news")), "title": item["title"],
            "link": safe_link(item["link"], feed), "published": item["published"],
        })
        return cand

    for feed, items in feed_items:
        news = bool(feed.get("news"))
        shop = shop_of(feed)
        # フィードはモールごと。別のモールのセール名と取り違えない
        shop_calendar = [e for e in calendar if shop_of(e) == shop]
        for item in items:
            age = (today - item["published"]).days
            if feed.get("mall"):
                if -1 <= age <= MALL_MAX_AGE_DAYS:
                    _detect_mall(item, feed, shop, shop_calendar, registered, events, add)
                continue
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
                # 日付の無い公式の発表も、カレンダーのセールなら知らせる (navi-brain#206 R1-4)。
                # 報道は Amazon 以外の記事が多いので、今までどおり日付が要る
                if entry and not news and DATELESS_OPEN_RE.search(
                        unicodedata.normalize("NFKC", item["title"])):
                    hit = calendar_match(entry["name"], [entry], item["published"])
                    if hit is None:
                        continue
                    event_id = "{}-{}".format(entry["id"], hit[1])
                    if event_id in registered or registered_by_name(
                            entry["name"], shop, item["published"], None, events):
                        logger.info("登録済みとして読み飛ばす: %s (%s)", event_id, item["title"])
                        continue
                    add(event_id, entry, shop, name, hit[1], feed, item)
                continue
            last_day = period["end"] or period["start"] + dt.timedelta(days=MAX_SPAN_DAYS)
            if last_day < today:
                continue
            event_id, label = event_key(entry, name, period["start"])
            if event_id in registered or _covered(period["start"], shop, events, name):
                logger.info("登録済みとして読み飛ばす: %s (%s)", event_id, item["title"])
                continue
            cand = add(event_id, entry, shop, name, label, feed, item)
            # 自動登録の出典 (amazon_events) には、期間を読めた発表を使う
            cand["sources"][-1]["dated"] = True
            _merge_period(cand["periods"], period)
            if not news:
                # 自動登録の判定 (auto_eligible) は公式の発表だけで見る。報道の時刻で補わない
                _merge_period(cand["official_periods"], dict(period))
    for cand in found.values():
        cand["sources"].sort(key=lambda s: (s["published"], s["link"]))
        for key in ("periods", "official_periods", "reference_periods"):
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
    registered_events: Optional[Sequence[Dict[str, Any]]] = None,
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
    return detect(feed_items, calendar, registered, today, registered_spans,
                  registered_events), errors


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
        '  url: "{}"'.format(cand.get("url") or SALE_URLS.get(cand.get("shop", "amazon"), "")),
        # 楽天・Yahoo の名前は題名から切り出したもので、「:」などを含みうる。YAML の文字列にする
        "  name: {}".format(json.dumps(cand["name"], ensure_ascii=False)),
        # 報道は出典にしない (Amazon.co.jp の告知で確かめて書き直してもらう)
        "  source: {}".format("{} の記事 → Amazon.co.jp の告知で確認".format(source["feed"])
                               if source.get("news") else "{} の発表".format(source["feed"])),
        "  verified_on: ????-??-??",
        "  phases:",
        "    - label: 本番",
        "      headline: {}".format(json.dumps(cand["name"], ensure_ascii=False)),
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
    references = cand.get("reference_periods") or []
    if not cand["periods"]:
        # 日付を題名から読めなかった・参考の日付しか無い (楽天・Yahoo の発表など)
        lines += ["", "**題名から開催期間を読めませんでした。** 期間は出典か"
                      "モールのキャンペーンページで確かめてください。"]
        if references:
            lines += ["", "題名にあった日付（参考。開始日ではなく山場の日付のことがあります）:"]
            for ref in references:
                lines.append("- {}{}".format(
                    ref["start"].isoformat(),
                    "〜{}".format(ref["end"].isoformat()) if ref["end"] else " から"))
    blank = {"start": None, "end": None, "start_time": None, "end_time": None}
    for period in cand["periods"] or [blank]:
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
