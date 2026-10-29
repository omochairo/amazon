"""#9449: sale_announcements.py (公式フィードからセール日程の候補を作る) の単体テスト。

固定するのは:
1. 実際に出た題名 (2024-11〜2026-09 の 10 回分) から、開催期間を正しく読める
2. 同じフィードに混ざる「日付の違う関連の発表」を拾わない
3. 曜日が暦と合わない・期間が長すぎる・過去の日付は捨てる
4. 題名に無い時刻は埋めない (貼り付け用の YAML が、直さないとテストで落ちる形になる)
5. 登録済み・終了済み・古い発表は候補にしない
6. フィードの形式 (RSS 2.0 / RDF) と、読めないときに「発表なし」と区別して落ちること
7. data/sale_calendar.yaml の実物で、実際の題名がそれぞれのセールに対応する
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sale_announcements as sa  # noqa: E402
from scripts import sale_calendar_reminder as scr  # noqa: E402

D = dt.date
REAL_CALENDAR = scr.load_yaml(scr.CALENDAR)
ABOUT, PRTIMES, PHILEWEB = sa.FEEDS

# (発表日, 題名, セールの id, 開始, 終了, 開始時刻, 終了時刻)。題名は実際に出たもの
REAL_ANNOUNCEMENTS = [
    (D(2024, 11, 15), "「Amazon ブラックフライデー」を11月29日（金）から12月6日（金）まで開催",
     "black-friday", D(2024, 11, 29), D(2024, 12, 6), None, None),
    (D(2024, 12, 27), "Amazon、年始のお得なセール「Amazon 初売り」を2025年1月3日（金）から5日間開催",
     "hatsuuri", D(2025, 1, 3), D(2025, 1, 7), None, None),
    (D(2025, 2, 21),
     "Amazon、「Amazon スマイルSALE 新生活」を2月28日（金）9時から3月4日（火）23時59分まで開催",
     "shinseikatsu", D(2025, 2, 28), D(2025, 3, 4), (9, 0), (23, 59)),
    (D(2025, 6, 17), "Amazon、「プライムデー」を初の4日間開催へ：7月11日（金）0時から14日（月）23時59分まで",
     "prime-day", D(2025, 7, 11), D(2025, 7, 14), (0, 0), (23, 59)),
    (D(2025, 9, 16),
     "Amazon、プライム会員限定の特別なセールイベント「プライム感謝祭」を初の4日間開催へ："
     "10月7日（火）0時から10月10日（金）23時59分まで",
     "prime-kanshasai", D(2025, 10, 7), D(2025, 10, 10), (0, 0), (23, 59)),
    (D(2025, 11, 14),
     "Amazon、年末最後のビッグセール「Amazon ブラックフライデー」を"
     "11月24日（月）0時から12月1日（月）23時59分までの8日間開催",
     "black-friday", D(2025, 11, 24), D(2025, 12, 1), (0, 0), (23, 59)),
    (D(2025, 12, 27), "Amazon、年始のお得なセール「Amazon 初売り」を2026年1月3日（土）から5日間開催",
     "hatsuuri", D(2026, 1, 3), D(2026, 1, 7), None, None),
    (D(2026, 2, 13), "Amazon、「Amazon 新生活セール」を3月6日（金）0時から3月9日（月）23時59分まで4日間開催",
     "shinseikatsu", D(2026, 3, 6), D(2026, 3, 9), (0, 0), (23, 59)),
    (D(2026, 6, 16), "Amazon、「プライムデー」を7月10日（金）から13日（月）まで4日間開催",
     "prime-day", D(2026, 7, 10), D(2026, 7, 13), None, None),
    (D(2026, 9, 15),
     "Amazon、プライム会員限定の特別なセール「プライム感謝祭」を"
     "10月16日（金）0時から10月19日（月）23時59分まで開催",
     "prime-kanshasai", D(2026, 10, 16), D(2026, 10, 19), (0, 0), (23, 59)),
]

# 同じフィードに実際に出た、セールの開催日程ではない発表
REAL_NOISE = [
    (D(2025, 11, 14),
     "Amazon、ほしいが丸ごと見つかる、体験イベント 「Amazon Black Friday Market」を"
     "11月21日（金）から3日間限定で六本木ヒルズアリーナにて開催"),
    (D(2024, 11, 15),
     "Amazon、ブラックフライデーでのギフト選びのわくわく感を体験できるイベント"
     "「BLACK FRIDAY イルミネーション by Amazon」を11月22日（金）から3日間限定で新宿にオープン"),
    (D(2025, 6, 19),
     "Amazon、お客様一人ひとりの“欲しいもの”に出合える「Amazon プライムデー フェス」を"
     "6月27日（金）から3日間限定で六本木ヒルズアリーナにて開催"),
    (D(2025, 7, 2),
     "Amazonネットスーパー、食品や日用品をお得にご提供する「プライムデー特選セール」を"
     "7月11日（金）から7月14日（月）まで開催"),
    (D(2024, 11, 22),
     "11月29日（金）から12月6日（金）までAmazonネットスーパーで「ブラックフライデーセール」を開催。"
     "Amazonフレッシュでは対象商品が最大50%OFF"),
    (D(2025, 11, 14), "Amazon、「ドライバーさんにありがとうキャンペーン」を11月24日（月）より実施"),
    (D(2026, 6, 16),
     "Amazon、「プライムデー」の開催に向けた「今夏のお買い物に関する意識・実態調査」を実施"),
    (D(2026, 10, 1), "「プライム感謝祭」でAmazon Nowの配送料と小口注文手数料が0円に"),
    (D(2026, 6, 16),
     "Amazon、“今、必要な物・ほしい物”に出会える特別イベント「Amazon プライムデー ステージ」を開催"),
]


def _candidate(published, title):
    """題名 1 本を、未登録・発表当日として検知にかける。"""
    item = {"title": title, "link": "https://prtimes.jp/main/html/rd/p/1.html",
            "published": published}
    return sa.detect([(PRTIMES, [item])], REAL_CALENDAR, set(), published)


# ---- 1. 実際の題名を読める ----

@pytest.mark.parametrize("published, title, cal_id, start, end, start_time, end_time",
                         REAL_ANNOUNCEMENTS)
def test_real_announcement_titles(published, title, cal_id, start, end, start_time, end_time):
    found = _candidate(published, title)
    event_id = "{}-{}".format(cal_id, start.year)
    assert list(found) == [event_id]
    assert found[event_id]["periods"] == [
        {"start": start, "end": end, "start_time": start_time, "end_time": end_time}]


# ---- 2. 関連の発表を拾わない ----

@pytest.mark.parametrize("published, title", REAL_NOISE)
def test_related_releases_are_not_candidates(published, title):
    assert _candidate(published, title) == {}


def test_follow_up_release_with_the_same_period_is_merged():
    # 続報は同じ日程を題名に持つ。候補は 1 つのまま、出典が 2 本になる
    first = {"title": REAL_ANNOUNCEMENTS[5][1], "link": "", "published": D(2025, 11, 14)}
    follow = {"title": "Amazon、11月24日（月）から12月1日（月）まで開催する「Amazon ブラックフライデー」の"
                       "注目の各種セールとキャンペーンを発表",
              "link": "", "published": D(2025, 11, 18)}
    found = sa.detect([(PRTIMES, [follow, first])], REAL_CALENDAR, set(), D(2025, 11, 18))
    cand = found["black-friday-2025"]
    assert len(cand["periods"]) == 1
    # 時刻は書いてあった方を採る
    assert cand["periods"][0]["start_time"] == (0, 0)
    assert [s["published"] for s in cand["sources"]] == [D(2025, 11, 14), D(2025, 11, 18)]


def test_conflicting_periods_are_both_kept_and_flagged():
    a = {"title": "Amazon、「プライムデー」を7月10日（金）から13日（月）まで4日間開催",
         "link": "", "published": D(2026, 6, 16)}
    b = {"title": "Amazon、「プライムデー」を7月11日（土）から14日（火）まで4日間開催",
         "link": "", "published": D(2026, 6, 17)}
    cand = sa.detect([(PRTIMES, [a, b])], REAL_CALENDAR, set(), D(2026, 6, 17))["prime-day-2026"]
    assert len(cand["periods"]) == 2
    assert "食い違って" in "\n".join(sa.render_candidate(cand))


def test_title_naming_two_sales_is_ambiguous():
    title = "「プライムデー」と「プライム感謝祭」を7月10日（金）から13日（月）まで開催"
    assert sa.match_sale(title, REAL_CALENDAR) is None


# ---- 3. 暦と合わないものは捨てる ----

@pytest.mark.parametrize("title", [
    "「プライムデー」を7月10日（木）から13日（月）まで開催",       # 開始の曜日が違う
    "「プライムデー」を7月10日（金）から13日（火）まで開催",       # 終了の曜日が違う
    "「プライムデー」を7月10日（金）から8月10日（月）まで開催",    # 31 日間: 長すぎる
    "「プライムデー」を7月10日（金）から20日間開催",               # 長すぎる
    "「プライムデー」を6月12日（金）から15日（月）まで開催",       # 発表より前
    "「プライムデー」を2月30日（金）から3月2日（月）まで開催",     # 無い日付
    "「プライムデー」を7月10日（金）から13日（月）まで実施",       # 「開催」が無い
    "「プライムデー」は7月10日（金）に開催",                       # 期間が無い
])
def test_implausible_periods_are_rejected(title):
    assert sa.parse_period(title, D(2026, 6, 16)) is None


def test_year_is_inferred_from_weekday_across_new_year():
    # 12 月に出た「1月3日（日）」は翌年。曜日が合う年だけを採る
    period = sa.parse_period("「Amazon 初売り」を1月3日（日）9時から1月7日（木）23時59分まで開催",
                             D(2026, 12, 27))
    assert (period["start"], period["end"]) == (D(2027, 1, 3), D(2027, 1, 7))
    assert period["start_time"] == (9, 0)


def test_holiday_suffix_and_halfwidth_are_accepted():
    period = sa.parse_period("「Amazon ブラックフライデー」を 11月24日(月・祝) 0時から 12月1日(月) 23時59分まで開催",
                             D(2025, 11, 14))
    assert (period["start"], period["end"]) == (D(2025, 11, 24), D(2025, 12, 1))


# ---- 4. 題名に無い時刻は埋めない ----

def test_yaml_without_times_fails_the_sale_events_checks():
    cand = _candidate(*REAL_ANNOUNCEMENTS[6][:2])["hatsuuri-2026"]
    text = sa.render_yaml(cand, cand["periods"][0])
    assert '"2026-01-03T??:??:??+09:00"' in text and '"2026-01-07T??:??:??+09:00"' in text
    event = yaml.safe_load(text)[0]
    assert event["id"] == "hatsuuri-2026"
    # そのまま貼ると test_sale_events.py が落とす形であること
    with pytest.raises(ValueError):
        dt.datetime.fromisoformat(event["phases"][0]["start"])
    with pytest.raises(ValueError):
        dt.date.fromisoformat(event["verified_on"])


def test_yaml_with_times_is_parseable_except_verified_on():
    cand = _candidate(*REAL_ANNOUNCEMENTS[9][:2])["prime-kanshasai-2026"]
    event = yaml.safe_load(sa.render_yaml(cand, cand["periods"][0]))[0]
    phase = event["phases"][0]
    assert phase["start"] == "2026-10-16T00:00:00+09:00"
    assert phase["end"] == "2026-10-19T23:59:59+09:00"
    assert event["verified_on"] == "????-??-??"


def test_rendered_title_cannot_inject_markdown():
    item = {"title": "「プライムデー」を7月10日（金）から13日（月）まで開催 `x` @someone\n# 見出し <b>",
            "link": "https://evil.example/x", "published": D(2026, 6, 16)}
    cand = sa.detect([(PRTIMES, [item])], REAL_CALENDAR, set(), D(2026, 6, 16))["prime-day-2026"]
    line = next(l for l in sa.render_candidate(cand) if l.startswith("- 2026-06-16"))
    # 題名は 1 つのコードスパンに収まり、リンクはフィードのホスト以外を通さない
    assert line.count("`") == 2 and "\n" not in line
    assert "evil.example" not in line and PRTIMES["url"] in line


# ---- 5. 候補にしないもの ----

def test_registered_event_is_not_a_candidate():
    published, title = REAL_ANNOUNCEMENTS[9][:2]
    item = {"title": title, "link": "", "published": published}
    assert sa.detect([(ABOUT, [item])], REAL_CALENDAR, {"prime-kanshasai-2026"}, published) == {}


@pytest.mark.parametrize("today, expected", [
    (D(2026, 6, 14), []),                     # 発表日が 2 日以上先: 読まない
    (D(2026, 6, 16), ["prime-day-2026"]),
    (D(2026, 7, 13), ["prime-day-2026"]),     # 最終日までは候補
    (D(2026, 7, 14), []),                     # 終わったセールは候補にしない
])
def test_only_current_announcements(today, expected):
    published, title = REAL_ANNOUNCEMENTS[8][:2]
    item = {"title": title, "link": "", "published": published}
    assert list(sa.detect([(PRTIMES, [item])], REAL_CALENDAR, set(), today)) == expected


def test_old_announcement_is_ignored():
    item = {"title": "「プライムデー」を7月10日（金）から13日（月）まで開催", "link": "",
            "published": D(2026, 4, 12)}
    assert sa.detect([(PRTIMES, [item])], REAL_CALENDAR, set(), D(2026, 7, 1)) == {}


# ---- 6. フィード ----

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>About Amazon</title>
<item><title>Amazon、「プライムデー」を7月10日（金）から13日（月）まで4日間開催</title>
<link>https://www.aboutamazon.jp/news/retail/prime-day-2026?utm_source=rss</link>
<pubDate>Mon, 15 Jun 2026 23:30:00 GMT</pubDate></item>
<item><title>日付の無い項目</title><link>https://www.aboutamazon.jp/x</link></item>
</channel></rss>""".encode("utf-8")

RDF = """<?xml version="1.0" encoding="UTF-8" ?>
<rdf:RDF xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<channel rdf:about="https://prtimes.jp/"><title>PR TIMES</title></channel>
<item rdf:about="https://prtimes.jp/main/html/rd/p/000002145.000004612.html">
<title>Amazon、「プライムデー」を7月10日（金）から13日（月）まで4日間開催</title>
<link>https://prtimes.jp/main/html/rd/p/000002145.000004612.html</link>
<dc:date>2026-06-16T13:00:00+09:00</dc:date></item>
</rdf:RDF>""".encode("utf-8")


def test_parse_rss_converts_pubdate_to_jst_and_skips_undated_items():
    items = sa.parse_feed(RSS)
    # GMT 6/15 23:30 は JST 6/16
    assert [(i["published"], i["link"]) for i in items] == [
        (D(2026, 6, 16), "https://www.aboutamazon.jp/news/retail/prime-day-2026?utm_source=rss")]


def test_parse_rdf():
    items = sa.parse_feed(RDF)
    assert [i["published"] for i in items] == [D(2026, 6, 16)]


@pytest.mark.parametrize("data", [
    b"<html><body>Just a moment...</body></html>",          # 遮断ページ
    b"not xml",
    b'<?xml version="1.0"?><rss><channel></channel></rss>',  # item が無い
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel><item/></channel></rss>',
])
def test_unreadable_feed_raises_instead_of_looking_empty(data):
    with pytest.raises(sa.FeedError):
        sa.parse_feed(data)


def test_safe_link_strips_query_and_rejects_other_hosts():
    assert sa.safe_link("https://www.aboutamazon.jp/news/a?utm_source=rss", ABOUT) == \
        "https://www.aboutamazon.jp/news/a"
    for bad in ("http://www.aboutamazon.jp/news/a", "https://example.com/a",
                "javascript:alert(1)", "", "https://www.aboutamazon.jp.evil.example/a"):
        assert sa.safe_link(bad, ABOUT) == ABOUT["url"]


def test_collect_reports_failed_feed_and_uses_the_other(monkeypatch):
    def fake_fetch(url):
        if url == ABOUT["url"]:
            raise sa.FeedError("取得に失敗: HTTP 403")
        return RDF
    monkeypatch.setattr(sa, "fetch", fake_fetch)
    found, errors = sa.collect(REAL_CALENDAR, set(), D(2026, 6, 16))
    assert list(found) == ["prime-day-2026"]
    assert len(errors) == 1 and ABOUT["name"] in errors[0]


def test_same_announcement_in_both_feeds_is_one_candidate(monkeypatch):
    monkeypatch.setattr(sa, "fetch", lambda url: RSS if url == ABOUT["url"] else RDF)
    found, errors = sa.collect(REAL_CALENDAR, set(), D(2026, 6, 16))
    assert errors == []
    cand = found["prime-day-2026"]
    # 報道のフィードにも同じ題名を返している (「Amazon、「プライムデー」」は報道でも拾う)
    assert len(cand["periods"]) == 1 and len(cand["sources"]) == len(sa.FEEDS)
    assert cand["sources"][0]["link"] == "https://prtimes.jp/main/html/rd/p/000002145.000004612.html"


# ---- 7. 実物のカレンダー ----

def test_real_calendar_names_are_distinct_after_normalization():
    names = [n for e in REAL_CALENDAR for n in sa.announce_names(e)]
    assert len(names) == len(set(names))


# ---- 8. カレンダーに無いセール・報道のフィード (2026-11 のスマイルSALE の取り逃し) ----

# PHILE WEB に実際に出た題名 (2026-10-29)
PHILEWEB_SMILE = "Amazon「スマイルSALE」11/4 9時スタート。セール品情報など事前公開中"


def _news(published, title, registered=(), spans=()):
    item = {"title": title, "link": "http://www.phileweb.com/news/d-av/202610/29/1.html",
            "published": published}
    return sa.detect([(PHILEWEB, [item])], REAL_CALENDAR, set(registered), published, spans)


def test_monthly_smile_sale_is_found_in_news_without_end_date():
    found = _news(D(2026, 10, 29), PHILEWEB_SMILE)
    assert list(found) == ["smile-sale-2026-11"]
    cand = found["smile-sale-2026-11"]
    assert cand["year"] == "2026年11月"
    assert cand["periods"] == [
        {"start": D(2026, 11, 4), "end": None, "start_time": (9, 0), "end_time": None}]
    assert cand["sources"][0]["link"] == "https://www.phileweb.com/news/d-av/202610/29/1.html"
    yaml_text = sa.render_yaml(cand, cand["periods"][0])
    assert 'start: "2026-11-04T09:00:00+09:00"' in yaml_text
    assert 'end: "????-??-??T??:??:??+09:00"' in yaml_text


def test_registered_monthly_sale_is_not_a_candidate():
    assert _news(D(2026, 10, 29), PHILEWEB_SMILE, registered={"smile-sale-2026-11"}) == {}
    # id が違っても、登録済みの期間に開始日が入っていれば登録済み
    assert _news(D(2026, 10, 29), PHILEWEB_SMILE,
                 spans=[(D(2026, 11, 4), D(2026, 11, 12))]) == {}


@pytest.mark.parametrize("title", [
    # Amazon と言い切っていない (他社のセール)
    "楽天「ブラックフライデー」11/21 20時スタート",
    "ヨドバシ「スマイルSALE」11/4 9時スタート",
    # Amazon の記事だが、セールの発表ではない
    "【本日限定】レグザ85V型4K液晶テレビが20万円以下！Amazonタイムセールで30％オフ",
    "【限定数残り半分以下】AnkerのUSB充電器＆USBケーブルセットがAmazonでセール中",
    # 日付が無い
    "Amazon「スマイルSALE」で狙い目の4Kテレビ",
])
def test_news_noise_is_not_a_candidate(title):
    assert _news(D(2026, 10, 29), title) == {}


@pytest.mark.parametrize("published, title", REAL_NOISE)
def test_official_noise_is_not_a_candidate_from_news_either(published, title):
    assert _news(published, title) == {}


def test_unknown_sale_in_official_feed_becomes_adhoc():
    title = "Amazon、「Amazon タイムセール祭り」を12月5日（土）9時から12月8日（火）23時59分まで開催"
    found = _candidate(D(2026, 11, 27), title)
    assert list(found) == ["adhoc-2026-12-05"]
    cand = found["adhoc-2026-12-05"]
    assert cand["name"] == "Amazon タイムセール祭り"
    assert cand["calendar_id"] is None
    assert cand["periods"] == [
        {"start": D(2026, 12, 5), "end": D(2026, 12, 8), "start_time": (9, 0), "end_time": (23, 59)}]


def test_unknown_sale_in_news_with_slash_range():
    title = "Amazon「ビッグセール」11/20(金)〜11/23(月)開催"
    cand = _news(D(2026, 11, 10), title)["adhoc-2026-11-20"]
    assert cand["name"] == "Amazon ビッグセール"
    assert cand["periods"][0]["end"] == D(2026, 11, 23)


def test_known_sale_in_news_uses_calendar_id():
    title = "Amazon「ブラックフライデー」11/24(火)スタート"
    assert list(_news(D(2026, 11, 13), title)) == ["black-friday-2026"]


@pytest.mark.parametrize("title", [
    "Amazon「スマイルSALE」11/5(水)9時スタート",   # 曜日が暦と合わない (11/5 は木)
    "Amazon「スマイルSALE」9/4 9時スタート",        # 発表より前
])
def test_news_dates_are_still_checked(title):
    assert _news(D(2026, 10, 29), title) == {}


def test_start_only_and_full_period_are_merged():
    a = {"title": PHILEWEB_SMILE, "link": "", "published": D(2026, 10, 29)}
    b = {"title": "Amazon「スマイルSALE」11/4(水)9時〜11/12(木)23時59分",
         "link": "", "published": D(2026, 10, 30)}
    cand = sa.detect([(PHILEWEB, [a, b])], REAL_CALENDAR, set(),
                     D(2026, 10, 30))["smile-sale-2026-11"]
    assert cand["periods"] == [
        {"start": D(2026, 11, 4), "end": D(2026, 11, 12), "start_time": (9, 0),
         "end_time": (23, 59)}]
    assert "食い違って" not in "\n".join(sa.render_candidate(cand))


def test_start_only_candidate_expires_after_max_span():
    assert list(_news(D(2026, 10, 29), PHILEWEB_SMILE)) == ["smile-sale-2026-11"]
    item = {"title": PHILEWEB_SMILE, "link": "", "published": D(2026, 10, 29)}
    late = D(2026, 11, 4) + dt.timedelta(days=sa.MAX_SPAN_DAYS + 1)
    assert sa.detect([(PHILEWEB, [item])], REAL_CALENDAR, set(), late) == {}


# ---- 9. レビューで見つかった取り違え・取り逃し ----

@pytest.mark.parametrize("title", [
    "楽天「Amazon対抗セール」11/4 9時スタート",                 # 他社の売り文句
    "Amazon「スマイルSALE」11/4(水)〜11/12(水)開催",           # 終了の曜日が違う → 開始だけ拾わない
])
def test_misleading_news_titles_are_rejected(title):
    assert _news(D(2026, 10, 29), title) == {}


def test_comma_between_amazon_and_name_is_accepted():
    assert list(_news(D(2026, 10, 29), "Amazon、「スマイルSALE」11/4 9時スタート")) == [
        "smile-sale-2026-11"]


def test_monthly_sale_with_a_seasonal_suffix_is_the_monthly_sale():
    found = _news(D(2027, 4, 10), "Amazon「スマイルSALE ゴールデンウィーク」4/18 9時スタート")
    assert list(found) == ["smile-sale-2027-04"]
    # 完全一致の別のセール (新生活) はそちらが勝つ
    found = _news(D(2027, 2, 20), "Amazon「スマイルSALE 新生活」2/27 9時スタート")
    assert list(found) == ["shinseikatsu-2027"]


def test_adhoc_alias_of_a_calendar_sale_on_the_same_day_is_dropped():
    a = {"title": PHILEWEB_SMILE, "link": "", "published": D(2026, 10, 29)}
    b = {"title": "Amazon「スマイルセール」11/4 9時スタート", "link": "",
         "published": D(2026, 10, 29)}
    found = sa.detect([(PHILEWEB, [a, b])], REAL_CALENDAR, set(), D(2026, 10, 29))
    assert list(found) == ["smile-sale-2026-11"]
