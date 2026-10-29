"""#9449: shop_sale_watch.py (楽天・Yahoo の公式告知から日程を読み、正本に足す) の単体テスト。

ページは合成した最小の HTML (書き方は 2025-12〜2026-10 の実物に合わせた)。
読み取りの検査は omochairo/omcha-ops の tests/test_shop_sale_watch.py から移した。

固定するのは:
1. 決まった場所 (ポイントアップ期間 / 最終3日間) の期間だけ読み、曜日・長さで読み違いを落とす
2. ページが変わった・読めないときは失敗として返す (緑のまま検知が止まらない)
3. 人が書いたイベントを書き換えず、同じモールで登録済みの期間とは重ねない
4. 足した YAML が、そのまま test_sale_events.py の決まりを満たす
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
from scripts import shop_sale_watch as w  # noqa: E402

JST = w.JST
CALENDAR = scr.load_yaml(scr.CALENDAR)
MARATHON, SUPERSALE = w.RAKUTEN_PAGES


def at(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST)


def rakuten_html(title: str, body: str) -> str:
    return "<html><head><title>【楽天市場】{}│買えば買うほど</title></head><body>{}</body></html>".format(
        title, body)


MARATHON_ON = rakuten_html("お買い物マラソン", (
    "<dt>エントリー期間</dt><dd>2026年10月22日(木)10:00～2026年10月27日(火)09:59</dd>"
    "<dt>ポイントアップ期間</dt><dd>2026年10月24日(土)20:00～2026年10月27日(火)09:59</dd>"))
MARATHON_OFF = rakuten_html("お買い物マラソン", "<p>次回の開催をお待ちください</p>")


def news_html(items):
    li = "".join(
        '<li><a href="https://www.lycorp.co.jp/ja/news/release/{}/" class="c-article-panel-d2">'
        '<div class="txt"><time datetime="{}">x</time><p>{}</p></div></a></li>'.format(n, d, t)
        for n, d, t in items)
    return "<html><head><title>ニュース</title></head><body><ul>{}</ul></body></html>".format(li)


PAYPAY_A = ("<p>事前期間：2025年12月1日（月）12時00分～12日（金）23時59分</p>"
            "<p>最終3日間：2025年12月13日（土）0時00分～15日（月）25時59分</p>"
            "<p>くじ引き期間：2025年12月1日（月）12時00分～15日（月）25時59分</p>")
PAYPAY_B = ("<p>くじ券獲得期間：2026年7月1日（水）12時00分～9日（木）23時59分</p>"
            "<p>実施期間： 2026年7月10日（金）0時00分～12日（日）25時59分（最終3日間）</p>")


def span(events):
    return [(e["start"].strftime("%Y-%m-%d %H:%M:%S"), e["end"].strftime("%Y-%m-%d %H:%M:%S"))
            for e in events]


# --- 楽天 ---------------------------------------------------------------------

def test_rakuten_reads_point_up_period_only():
    ev = w.parse_rakuten(MARATHON_ON, MARATHON, at("2026-10-22 12:00"))
    # エントリー期間は拾わない。終わりは「09:59 まで」= 09:59:59
    assert span(ev) == [("2026-10-24 20:00:00", "2026-10-27 09:59:59")]
    assert ev[0]["id"] == "rakuten-marathon-2026-10-24"
    assert ev[0]["shop"] == "rakuten"
    assert ev[0]["name"] == "楽天市場 お買い物マラソン"


def test_rakuten_without_period_is_normal():
    assert w.parse_rakuten(MARATHON_OFF, MARATHON, at("2026-10-22 12:00")) == []


def test_rakuten_weekday_mismatch_is_dropped():
    wrong = MARATHON_ON.replace("2026年10月24日(土)", "2026年10月24日(日)")
    assert w.parse_rakuten(wrong, MARATHON, at("2026-10-22 12:00")) == []


def test_rakuten_too_long_period_is_dropped():
    far = MARATHON_ON.replace("2026年10月27日(火)09:59</dd>", "2026年12月27日(日)09:59</dd>")
    assert w.parse_rakuten(far, MARATHON, at("2026-10-22 12:00")) == []


def test_rakuten_page_changed_is_error():
    with pytest.raises(w.SourceError):
        w.parse_rakuten(MARATHON_ON, SUPERSALE, at("2026-10-22 12:00"))


def test_supersale_same_format():
    page = rakuten_html("楽天スーパーSALE",
                        "<p>ポイントアップ期間 2026年12月4日(金)20:00～2026年12月11日(金)01:59</p>")
    ev = w.parse_rakuten(page, SUPERSALE, at("2026-11-20 12:00"))
    assert [e["id"] for e in ev] == ["rakuten-supersale-2026-12-04"]
    assert span(ev) == [("2026-12-04 20:00:00", "2026-12-11 01:59:59")]


# --- Yahoo --------------------------------------------------------------------

def test_news_list_exact_name_and_recent_only():
    lst = w.parse_news_list(news_html([
        ("020600", "2026-06-30", "Yahoo!ショッピングの「超PayPay祭」を本日から開催"),
        ("020601", "2026-06-29", "Yahoo!おトク宝箱「超PayPay祭 宝箱くじ」を実施"),
        ("019000", "2026-03-03", "春の「超PayPay祭」を本日から開催"),
        ("020602", "2026-06-28", "ヤフコメまとめを開始"),
    ]), at("2026-07-01 12:00"))
    assert [x["url"].rsplit("/", 2)[-2] for x in lst] == ["020600"]


def test_news_list_changed_is_error():
    with pytest.raises(w.SourceError):
        w.parse_news_list("<html><body>何も無い</body></html>", at("2026-07-01 12:00"))


def test_paypay_final_three_days():
    ev = w.parse_paypay_release(PAYPAY_A, "https://www.lycorp.co.jp/ja/news/release/019898/",
                                at("2025-12-01 13:00"))
    # 事前期間・くじは拾わない。25時59分は翌日 1:59
    assert span(ev) == [("2025-12-13 00:00:00", "2025-12-16 01:59:59")]
    assert ev[0]["shop"] == "yahoo"


def test_paypay_jisshi_kikan_form():
    ev = w.parse_paypay_release(PAYPAY_B, "u", at("2026-06-26 13:00"))
    assert [e["id"] for e in ev] == ["yahoo-paypay-final-2026-07-10"]
    assert span(ev) == [("2026-07-10 00:00:00", "2026-07-13 01:59:59")]


def test_paypay_without_final_days_is_empty():
    assert w.parse_paypay_release("<p>本日から開催</p>", "u", at("2026-07-01 12:00")) == []


def test_paypay_crosses_new_year():
    page = "<p>最終3日間：2026年12月31日（木）0時00分～1月2日（土）23時59分</p>"
    ev = w.parse_paypay_release(page, "u", at("2026-12-20 12:00"))
    assert span(ev) == [("2026-12-31 00:00:00", "2027-01-02 23:59:59")]


# --- 集める -------------------------------------------------------------------

PAGES = {
    MARATHON["url"]: MARATHON_ON,
    SUPERSALE["url"]: rakuten_html("楽天スーパーSALE", "<p>近日公開</p>"),
    w.LYCORP_NEWS: news_html([("020600", "2026-10-20",
                               "Yahoo!ショッピングの「超PayPay祭」を本日から開催")]),
    "https://www.lycorp.co.jp/ja/news/release/020600/":
        "<p>最終3日間：2026年11月13日（金）0時00分～15日（日）25時59分</p>",
}


def fake(pages):
    def get(url):
        if url not in pages:
            raise w.SourceError("取得に失敗: {}".format(url))
        return pages[url]
    return get


def test_collect_reads_both_shops():
    found, errors = w.collect(at("2026-10-22 12:00"), get=fake(PAGES))
    assert sorted(e["id"] for e in found) == [
        "rakuten-marathon-2026-10-24", "yahoo-paypay-final-2026-11-13"]
    assert errors == []


def test_collect_one_failure_keeps_others():
    pages = dict(PAGES)
    del pages[SUPERSALE["url"]]
    found, errors = w.collect(at("2026-10-22 12:00"), get=fake(pages))
    assert len(found) == 2 and len(errors) == 1


def test_collect_drops_finished():
    found, _ = w.collect(at("2026-10-28 12:00"), get=fake(PAGES))
    assert [e["id"] for e in found] == ["yahoo-paypay-final-2026-11-13"]


# --- Amazon (公式フィードの候補) ----------------------------------------------

def _amazon(feed_index, published, title):
    feed = sa.FEEDS[feed_index]
    item = {"title": title, "link": "https://prtimes.jp/main/html/rd/p/000001.html"
            if feed_index == 1 else "http://www.phileweb.com/news/1.html", "published": published}
    return w.amazon_events(sa.detect([(feed, [item])], CALENDAR, set(), published))


def test_amazon_official_with_full_times_is_registered():
    ev = _amazon(1, dt.date(2026, 9, 15),
                 "Amazon、プライム会員限定の特別なセール「プライム感謝祭」を"
                 "10月16日（金）0時から10月19日（月）23時59分まで開催")
    assert [e["id"] for e in ev] == ["prime-kanshasai-2026"]
    assert span(ev) == [("2026-10-16 00:00:00", "2026-10-19 23:59:59")]
    assert ev[0]["shop"] == "amazon" and ev[0]["source"].startswith("https://prtimes.jp/")


def test_amazon_without_times_goes_to_issue():
    # 時刻が題名に無い (初売りは 9 時開始の年がある) → 自動では足さない
    assert _amazon(1, dt.date(2026, 6, 16),
                   "Amazon、「プライムデー」を7月10日（金）から13日（月）まで4日間開催") == []


def test_amazon_news_only_goes_to_issue():
    # 報道だけの発表は自動では足さない
    assert _amazon(2, dt.date(2026, 10, 29),
                   "Amazon「スマイルSALE」11/4 9時スタート。セール品情報など事前公開中") == []


# --- 正本に足す ---------------------------------------------------------------

def _found():
    return w.collect(at("2026-10-22 12:00"), get=fake(PAGES))[0]


def test_plan_adds_new_ones():
    add, conflicts = w.plan(_found(), [])
    assert [f["id"] for f in add] == ["rakuten-marathon-2026-10-24", "yahoo-paypay-final-2026-11-13"]
    assert conflicts == []


def test_plan_skips_period_registered_by_hand_for_same_shop_only():
    events = [
        # 人が別の id で書いた楽天のマラソン → 足さない
        {"id": "marathon-by-hand", "shop": "rakuten",
         "phases": [{"start": "2026-10-24T20:00:00+09:00", "end": "2026-10-27T09:59:59+09:00"}]},
        # 同じ時期の Amazon のセール → Yahoo には関係ない
        {"id": "smile", "shop": "amazon",
         "phases": [{"start": "2026-11-10T00:00:00+09:00", "end": "2026-11-20T23:59:59+09:00"}]},
    ]
    add, _ = w.plan(_found(), events)
    assert [f["id"] for f in add] == ["yahoo-paypay-final-2026-11-13"]


def test_plan_does_not_rewrite_and_reports_changed_period():
    events = [{"id": "rakuten-marathon-2026-10-24", "shop": "rakuten",
               "phases": [{"start": "2026-10-24T20:00:00+09:00",
                           "end": "2026-10-27T01:59:59+09:00"}]}]
    add, conflicts = w.plan(_found(), events)
    assert "rakuten-marathon-2026-10-24" not in [f["id"] for f in add]
    assert len(conflicts) == 1 and "rakuten-marathon-2026-10-24" in conflicts[0]


def test_plan_same_period_is_quiet():
    events = [{"id": "rakuten-marathon-2026-10-24", "shop": "rakuten",
               "phases": [{"start": "2026-10-24T20:00:00+09:00",
                           "end": "2026-10-27T09:59:59+09:00"}]}]
    _, conflicts = w.plan(_found(), events)
    assert conflicts == []


def test_appended_yaml_passes_sale_events_rules(tmp_path):
    path = tmp_path / "sale_events.yaml"
    path.write_text("# 冒頭のコメント\n- id: keep\n  shop: amazon\n", encoding="utf-8")
    w.append_events(path, _found(), dt.date(2026, 10, 22))
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 冒頭のコメント\n- id: keep\n")
    events = yaml.safe_load(text)
    added = events[1:]
    assert [e["id"] for e in added] == ["rakuten-marathon-2026-10-24", "yahoo-paypay-final-2026-11-13"]
    for e in added:
        assert e["shop"] in ("rakuten", "yahoo") and e["source"].startswith("https://")
        assert e["verified_on"] == dt.date(2026, 10, 22)
        phase = e["phases"][0]
        # クォートした文字列で、+09:00 付き (test_sale_events.py の決まり)
        assert isinstance(phase["start"], str) and phase["start"].endswith("+09:00")
        assert dt.datetime.fromisoformat(phase["start"]) < dt.datetime.fromisoformat(phase["end"])
    # 読み直した期間と突き合わせても食い違わない (次の run で赤にならない)
    assert w.plan(_found(), events)[1] == []
