"""navi-brain#206: セールの取り逃し (2026-11 の楽天・Yahoo のブラックフライデー・Amazon の先行セール)
を受けた検知の作り直しのテスト。

固定するのは:
1. 楽天・Yahoo の PR TIMES の実際の題名 (2025-11〜2026-11) から、名前を決め打ちせずに
   大型セールの発表を拾い、別のサービス・セールでない発表は拾わない
2. 名前で「登録済み」とみなす判定が、短い語で別のセールに吸われない (取り逃しの再発を防ぐ)
3. 楽天・Yahoo の候補は自動登録の関門を通らない (日付は参考にだけ持つ)
4. 期間が重なるだけの別のセールを黙って捨てない (段階の開始日の前後 1 日で見る)
5. 自動登録したイベントを人が直したら、62 は突き合わせない
6. 楽天・Yahoo の例年のセールの目安のリマインドと、別の id での登録済みの判定
7. 自動登録した本番に先行セールを足す確認の issue
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sale_announcements as sa  # noqa: E402
from scripts import sale_calendar_reminder as scr  # noqa: E402
from scripts import shop_sale_watch as w  # noqa: E402

D = dt.date
JST = dt.timezone(dt.timedelta(hours=9))
REAL_CALENDAR = scr.load_yaml(scr.CALENDAR)
RAKUTEN_FEED = next(f for f in sa.FEEDS if f.get("mall") and f["shop"] == "rakuten")
YAHOO_FEED = next(f for f in sa.FEEDS if f.get("mall") and f["shop"] == "yahoo")

# PR TIMES の企業別 RSS に実際に出た題名 (2026-11-23 に取得)
RAKUTEN_BF = ("楽天、「楽天市場」「楽天ブックス」「楽天ラクマ」「楽天リーベイツ（Rebates）」"
              "においてブラックフライデーセールを開催")
RAKUTEN_HATSUURI = "「楽天市場」、2026年の「Rakuten初売り」を開催"
RAKUTEN_GENRE = "「楽天市場」、商品ジャンルごとのキャンペーンが集結する新セールイベント「ジャンル祭」を開催"
YAHOO_BF = "【Yahoo!ショッピング】年に一度のおトク満載のビッグチャンス「ブラックフライデー」を11月22日から開催"
YAHOO_BAKUGAI = "【Yahoo!ショッピング】9月11日から13日は誰でも最大21％戻ってくる「爆買ウルトラSALE」を本日から開催"
YAHOO_PAYPAY = "夏のお買い物を応援するYahoo!ショッピングの「超PayPay祭」を本日から開催。7月10日～12日は誰でも最大22％"

NOT_SALES = [
    ("rakuten", "「楽天ふるさと納税」、リアルイベント「楽天超ふるさと納税祭」の体験コンテンツや試食・飲食ブース情報等を新たに公開"),
    ("rakuten", "「楽天市場」、2026年冬のトレンド予測を発表"),
    ("rakuten", "「楽天市場」、AI活用による「楽天スーパーSALE」のお買い物体験を強化"),
    ("yahoo", "【Yahoo!トラベル】人気宿泊施設＋航空券プランがお得になる年に一度の大型企画「ヤフーパック ブラックフライデー」を11月16日から開催"),
    ("yahoo", "【LINEギフト】11月2日より「ブラックフライデー特集」を公開"),
    ("yahoo", "【Yahoo!ショッピング】「ブラックフライデー」のおトクな買い方をAIが提案する「あなただけのAIおトクスケジュール」を提供開始"),
    ("yahoo", "【Yahoo!フリマ】エンタメカテゴリの出品・購入がお得に楽しめる「エンタメ祭」を3月2日から初開催"),
]


def _item(title, published, link="https://prtimes.jp/main/html/rd/p/1.html"):
    return {"title": title, "link": link, "published": published}


def _mall(feed, title, published, today=None, registered=(), events=(), calendar=None):
    return sa.detect([(feed, [_item(title, published)])],
                     REAL_CALENDAR if calendar is None else calendar,
                     set(registered), today or published, registered_events=list(events))


def _event(event_id, shop, name, start, end, auto=None):
    ev = {"id": event_id, "shop": shop, "name": name,
          "phases": [{"start": start, "end": end}]}
    if auto:
        ev["auto"] = auto
    return ev


# ---- 1. 拾う・拾わない ----

@pytest.mark.parametrize("shop, title, name", [
    ("rakuten", RAKUTEN_BF, "ブラックフライデーセール"),
    ("rakuten", RAKUTEN_HATSUURI, "Rakuten初売り"),
    ("rakuten", RAKUTEN_GENRE, "ジャンル祭"),
    ("yahoo", YAHOO_BF, "ブラックフライデー"),
    ("yahoo", YAHOO_BAKUGAI, "爆買ウルトラSALE"),
    ("yahoo", YAHOO_PAYPAY, "超PayPay祭"),
])
def test_real_mall_sale_titles_are_found(shop, title, name):
    assert sa.mall_sale_name(title, shop) == name


@pytest.mark.parametrize("shop, title", NOT_SALES)
def test_other_services_and_non_sale_titles_are_not_found(shop, title):
    assert sa.mall_sale_name(title, shop) is None


def test_mall_name_of_another_shop_is_not_found():
    assert sa.mall_sale_name(YAHOO_BF, "rakuten") is None
    assert sa.mall_sale_name(RAKUTEN_BF, "yahoo") is None


# ---- 2. 名前で同じセールか ----

@pytest.mark.parametrize("a, b, same", [
    ("ブラックフライデーセール", "楽天市場 ブラックフライデー", True),
    ("Rakuten BLACK FRIDAY", "楽天市場 ブラックフライデー", True),
    ("Rakuten初売り", "楽天市場 初売り", True),
    ("楽天スーパーSALE", "楽天スーパーSALE（12月）", True),
    ("超PayPay祭", "Yahoo!ショッピング 超PayPay祭 最終3日間", True),
    ("ジャンル祭", "楽天市場 ジャンル祭", True),
    # 短い語・一部が同じだけの別のセール
    ("楽天スーパーSALE", "楽天市場 スーパーDEAL祭", False),
    ("楽天市場 大感謝祭", "ショップ感謝祭", False),
    ("Amazon プライム感謝祭", "楽天市場 大感謝祭", False),
    ("楽天市場セール", "楽天市場 ブラックフライデー", False),
    ("楽天市場", "楽天市場", False),
    ("", "", False),
])
def test_same_sale(a, b, same):
    assert sa.same_sale(a, b) is same


# ---- 3. 楽天・Yahoo の候補 ----

def test_rakuten_black_friday_becomes_calendar_candidate_without_dates():
    found = _mall(RAKUTEN_FEED, RAKUTEN_BF, D(2026, 11, 12))
    assert list(found) == ["rakuten-blackfriday-2026"]
    cand = found["rakuten-blackfriday-2026"]
    assert cand["shop"] == "rakuten" and cand["name"] == "楽天市場 ブラックフライデー"
    assert cand["periods"] == [] and cand["official_periods"] == []
    assert not sa.auto_eligible(cand)
    text = "\n".join(sa.render_candidate(cand))
    assert "題名から開催期間を読めませんでした" in text
    assert 'url: "https://event.rakuten.co.jp/campaign/point-up/blackfriday/"' in text
    assert 'start: "????-??-??T??:??:??+09:00"' in text


def test_yahoo_black_friday_keeps_title_date_only_as_reference():
    found = _mall(YAHOO_FEED, YAHOO_BF, D(2026, 11, 2))
    cand = found["yahoo-blackfriday-2026"]
    assert cand["periods"] == [] and not sa.auto_eligible(cand)
    assert [p["start"] for p in cand["reference_periods"]] == [D(2026, 11, 22)]
    assert "- 2026-11-22 から" in "\n".join(sa.render_candidate(cand))


def test_hatsuuri_announced_in_december_is_next_years_sale():
    found = _mall(RAKUTEN_FEED, RAKUTEN_HATSUURI, D(2025, 12, 26))
    assert list(found) == ["rakuten-hatsuuri-2026"]


def test_sale_outside_calendar_gets_adhoc_id_by_month_and_name():
    found = _mall(YAHOO_FEED, YAHOO_PAYPAY, D(2026, 6, 26))
    (event_id, cand), = found.items()
    assert event_id.startswith("adhoc-yahoo-2026-06-")
    assert cand["name"] == "Yahoo!ショッピング 超PayPay祭"
    # 「本日から」は発表日を参考の開始日にする
    assert [p["start"] for p in cand["reference_periods"]] == [D(2026, 6, 26)]
    # 同じセールの第 2 報は同じ id (issue が立ち直らない)
    again = _mall(YAHOO_FEED, "【Yahoo!ショッピング】「超PayPay祭」を本日より開催", D(2026, 6, 29))
    assert list(again) == [event_id]


def test_two_sales_on_the_same_day_are_two_candidates():
    items = [_item(YAHOO_PAYPAY, D(2026, 6, 26), "https://prtimes.jp/main/html/rd/p/1.html"),
             _item("【Yahoo!ショッピング】「ジャンル祭」を本日から開催", D(2026, 6, 26),
                   "https://prtimes.jp/main/html/rd/p/2.html")]
    found = sa.detect([(YAHOO_FEED, items)], REAL_CALENDAR, set(), D(2026, 6, 26))
    assert len(found) == 2


def test_old_mall_announcements_are_not_candidates():
    assert _mall(RAKUTEN_FEED, RAKUTEN_GENRE, D(2026, 10, 2), today=D(2026, 10, 17)) == {}
    assert list(_mall(RAKUTEN_FEED, RAKUTEN_GENRE, D(2026, 10, 2), today=D(2026, 10, 16)))


def test_registered_by_id_or_by_name_and_time():
    registered = _event("rakuten-blackfriday-2026", "rakuten", "楽天市場 ブラックフライデー",
                        "2026-11-19T20:00:00+09:00", "2026-11-25T01:59:59+09:00")
    events = scr.registered_events([registered])
    assert _mall(RAKUTEN_FEED, RAKUTEN_BF, D(2026, 11, 12),
                 registered={"rakuten-blackfriday-2026"}) == {}
    # id が違っても (人が別の id で書いた)、名前と時期が合えば登録済み
    renamed = dict(events[0], id="rakuten-bf-by-hand")
    assert _mall(RAKUTEN_FEED, RAKUTEN_BF, D(2026, 11, 12), events=[renamed]) == {}


def test_same_name_in_another_round_does_not_hide_candidate():
    # 6 月の回が登録済みでも、7 月の回の発表 (題名に 7/10) は知らせる
    june = scr.registered_events([_event(
        "yahoo-paypay-final-2026-06-20", "yahoo", "Yahoo!ショッピング 超PayPay祭 最終3日間",
        "2026-06-20T00:00:00+09:00", "2026-06-23T01:59:00+09:00")])
    found = _mall(YAHOO_FEED, "【Yahoo!ショッピング】「超PayPay祭」を7月10日から開催",
                  D(2026, 6, 21), events=june)
    assert len(found) == 1
    # 終わった回は、その後の発表を黙らせない
    found = _mall(YAHOO_FEED, YAHOO_PAYPAY, D(2026, 6, 26), events=june)
    assert len(found) == 1


def test_short_name_does_not_hide_another_sale():
    # 「スーパーSALE」の登録で「スーパーDEAL祭」を黙らせない
    supersale = scr.registered_events([_event(
        "rakuten-supersale-2026-12-04", "rakuten", "楽天スーパーSALE",
        "2026-12-04T20:00:00+09:00", "2026-12-11T01:59:59+09:00")])
    found = _mall(RAKUTEN_FEED, "「楽天市場」、「スーパーDEAL祭」を開催", D(2026, 12, 1),
                  events=supersale)
    assert len(found) == 1


def test_mall_candidates_are_never_auto_registered():
    # 日時が全部題名にあっても、楽天・Yahoo の発表は自動登録しない (関門は今のまま)
    title = "【Yahoo!ショッピング】「ブラックフライデー」を11月22日（日）0時から11月29日（日）23時59分まで開催"
    found = _mall(YAHOO_FEED, title, D(2026, 11, 2))
    assert w.amazon_events(found) == []


# ---- Amazon の日付の無い公式の発表 ----

def test_amazon_official_title_without_dates_is_a_candidate():
    prtimes = sa.FEEDS[1]
    found = sa.detect([(prtimes, [_item("Amazon、年末最後のビッグセール「Amazon ブラックフライデー」を開催",
                                        D(2027, 11, 12))])], REAL_CALENDAR, set(), D(2027, 11, 12))
    assert list(found) == ["black-friday-2027"]
    assert found["black-friday-2027"]["periods"] == []
    assert sa.detect([(prtimes, [_item("Amazon、年末最後のビッグセール「Amazon ブラックフライデー」を開催",
                                       D(2027, 11, 12))])], REAL_CALENDAR, {"black-friday-2027"},
                     D(2027, 11, 12)) == {}


# ---- 4・5. 62 の plan ----

def _found_marathon(start="2026-11-21T20:00:00+09:00"):
    s = dt.datetime.fromisoformat(start)
    return [{"id": "rakuten-marathon-" + s.date().isoformat(), "shop": "rakuten",
             "name": "楽天市場 お買い物マラソン", "source": "x", "url": "x",
             "start": s, "end": s + dt.timedelta(days=3)}]


def test_plan_adds_sale_that_only_overlaps_another_one():
    bf = _event("rakuten-blackfriday-2026", "rakuten", "楽天市場 ブラックフライデー",
                "2026-11-19T20:00:00+09:00", "2026-11-25T01:59:59+09:00")
    add, conflicts = w.plan(_found_marathon(), [bf])
    assert [f["id"] for f in add] == ["rakuten-marathon-2026-11-21"] and conflicts == []
    # 開始日が前後 1 日なら、人が別の id で書いた同じ回とみなす
    add, _ = w.plan(_found_marathon("2026-11-20T20:00:00+09:00"), [bf])
    assert add == []


def test_plan_skips_hand_edited_event_with_same_id():
    hand = _event("rakuten-marathon-2026-11-21", "rakuten", "楽天市場 お買い物マラソン",
                  "2026-11-21T10:00:00+09:00", "2026-11-25T01:59:59+09:00")
    add, conflicts = w.plan(_found_marathon(), [hand])
    assert add == [] and conflicts == []
    hand["auto"] = "shop_sale_watch"
    _, conflicts = w.plan(_found_marathon(), [hand])
    assert len(conflicts) == 1


# ---- 6. 楽天・Yahoo の目安のリマインド ----

def _rakuten_supersale_dec():
    return [e for e in REAL_CALENDAR if e["id"] == "rakuten-supersale-12"]


def test_supersale_reminder_waits_for_auto_registration():
    cal = _rakuten_supersale_dec()
    assert scr.due_reminders(cal, [], D(2026, 12, 2)) == []
    assert [r["event_id"] for r in scr.due_reminders(cal, [], D(2026, 12, 3))] == [
        "rakuten-supersale-12-2026"]


def test_reminder_counts_auto_registered_event_with_other_id():
    cal = _rakuten_supersale_dec()
    auto = _event("rakuten-supersale-2026-12-04", "rakuten", "楽天スーパーSALE",
                  "2026-12-04T20:00:00+09:00", "2026-12-11T01:59:59+09:00", auto="shop_sale_watch")
    assert scr.due_reminders(cal, [auto], D(2026, 12, 3)) == []
    # 別のセール (マラソン) が同じ時期に登録されていても、スーパーSALE の登録とはみなさない
    other = dict(auto, id="rakuten-marathon-2026-12-04", name="楽天市場 お買い物マラソン")
    assert len(scr.due_reminders(cal, [other], D(2026, 12, 3))) == 1


def test_mall_reminder_body_says_detection_has_not_found_it():
    row = scr.due_reminders(_rakuten_supersale_dec(), [], D(2026, 12, 3))[0]
    body = scr.render_body(row, D(2026, 12, 3))
    assert "まだこのセールを拾っていません" in body
    assert "https://event.rakuten.co.jp/campaign/supersale/" in body
    assert "楽天市場 の公式の告知" in body


def test_real_registered_mall_events_match_calendar_ids():
    # 2026-11-22 に手で登録した楽天・Yahoo のブラックフライデーは、カレンダーの id と一致する
    events = scr.load_yaml(scr.SALE_EVENTS)
    assert scr.due_reminders(REAL_CALENDAR, events, D(2026, 11, 20)) == []


# ---- 7. 先行セールの確認 ----

BF_AUTO = _event("black-friday-2027", "amazon", "Amazon ブラックフライデー",
                 "2027-11-26T00:00:00+09:00", "2027-12-03T23:59:59+09:00", auto="shop_sale_watch")


def test_presale_check_for_auto_registered_main_sale():
    rows = scr.presale_checks(REAL_CALENDAR, [BF_AUTO], D(2027, 11, 15))
    assert [r["event_id"] for r in rows] == ["black-friday-2027"]
    # 本番が始まったら・人が auto を消したら・段階を足したら対象外
    assert scr.presale_checks(REAL_CALENDAR, [BF_AUTO], D(2027, 11, 26)) == []
    assert scr.presale_checks(REAL_CALENDAR, [dict(BF_AUTO, auto=None)], D(2027, 11, 15)) == []
    two = dict(BF_AUTO, phases=BF_AUTO["phases"] * 2)
    assert scr.presale_checks(REAL_CALENDAR, [two], D(2027, 11, 15)) == []
    # pre_sale の無いセール (初売り) は対象外
    hatsuuri = _event("hatsuuri-2028", "amazon", "Amazon 初売り", "2028-01-03T09:00:00+09:00",
                      "2028-01-07T23:59:59+09:00", auto="shop_sale_watch")
    assert scr.presale_checks(REAL_CALENDAR, [hatsuuri], D(2027, 12, 28)) == []


class FakeGh:
    def __init__(self, existing=None, comments=None):
        self.existing = existing or {}
        self.comments = comments or []
        self.created, self.commented, self.searched = [], [], []

    def __call__(self, args):
        if args[:3] == ["api", "-X", "GET"] and "/comments" in args[3]:
            return json.dumps(self.comments)
        if args[:2] == ["api", "-X"]:
            query = next(a for a in args if a.startswith("q="))
            self.searched.append(query)
            for marker, items in self.existing.items():
                if '"{}"'.format(marker) in query:
                    return json.dumps({"items": items})
            return json.dumps({"items": []})
        if args[:2] == ["issue", "create"]:
            self.created.append(args)
            return "https://github.com/o/r/issues/1\n"
        if args[:2] == ["issue", "comment"]:
            self.commented.append(args)
            return "https://github.com/o/r/issues/7#issuecomment-1\n"
        raise AssertionError(args)


@pytest.fixture
def bf_files(tmp_path, monkeypatch):
    events = tmp_path / "sale_events.yaml"
    import yaml
    events.write_text(yaml.safe_dump([BF_AUTO], allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(scr, "SALE_EVENTS", events)
    # 実物のカレンダーの Amazon ブラックフライデーだけ (同じ時期の楽天・Yahoo の目安を混ぜない)
    cal = tmp_path / "sale_calendar.yaml"
    cal.write_text(yaml.safe_dump([e for e in REAL_CALENDAR if e["id"] == "black-friday"],
                                  allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(scr, "CALENDAR", cal)
    monkeypatch.setattr(sa, "collect", lambda *a, **k: ({}, []))
    return events


def _body(args):
    return args[args.index("--body") + 1]


def test_presale_issue_is_created_once(bf_files, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-11-15"]) == 0
    assert len(gh.created) == 1
    assert _body(gh.created[0]).startswith("<!-- sale-presale:black-friday-2027 -->")
    assert "先行セール" in gh.created[0][gh.created[0].index("--title") + 1]
    # 閉じた確認の issue があれば立て直さない
    marker = "sale-presale:black-friday-2027"
    gh = FakeGh(existing={marker: [{"number": 3, "state": "closed",
                                    "body": "<!-- {} -->".format(marker)}]})
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-11-15"]) == 0
    assert gh.created == [] and gh.commented == []


def test_presale_goes_to_open_reminder_issue_as_one_comment(bf_files, monkeypatch):
    reminder = "sale-calendar-reminder:black-friday-2027"
    open_issue = {reminder: [{"number": 7, "state": "open",
                              "body": "<!-- {} -->".format(reminder)}]}
    gh = FakeGh(existing=open_issue)
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-11-15"]) == 0
    assert gh.created == [] and len(gh.commented) == 1
    assert _body(gh.commented[0]).startswith("<!-- sale-presale:black-friday-2027 -->")
    gh = FakeGh(existing=open_issue,
                comments=[{"body": "<!-- sale-presale:black-friday-2027 -->\n..."}])
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-11-15"]) == 0
    assert gh.created == [] and gh.commented == []


def test_only_one_write_per_run_and_no_search_after_write(bf_files, monkeypatch):
    cand = {"event_id": "adhoc-yahoo-2027-11-abcdef", "calendar_id": None, "shop": "yahoo",
            "name": "Yahoo!ショッピング X祭", "year": "2027年11月", "periods": [],
            "official_periods": [], "reference_periods": [],
            "sources": [{"feed": "PR TIMES（LINEヤフー）", "news": False, "title": "t",
                         "link": "https://prtimes.jp/main/html/rd/p/1.html",
                         "published": D(2027, 11, 14)}]}
    monkeypatch.setattr(sa, "collect", lambda *a, **k: ({cand["event_id"]: cand}, []))
    gh = FakeGh()
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-11-15"]) == 0
    # 発表の候補が先。先行セールの確認は次の run に回し、検索もしない
    assert len(gh.created) == 1 and "adhoc-yahoo-2027-11-abcdef" in _body(gh.created[0])
    assert len(gh.searched) == 1
