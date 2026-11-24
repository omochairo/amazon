"""announce_shadow (navi-brain#210 の影運転) のテスト。"""
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import announce_shadow as a  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

RSS1 = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>メーカー</title></channel>
<item><title>知育玩具「くるくるチャイム ミニ」を12月に新発売</title><link>https://prtimes.jp/a/1</link>
<dc:date>2026-11-24T09:00:00+09:00</dc:date><description>&lt;p&gt;本文&lt;/p&gt;</description></item>
<item><title>古い発表「むかしの商品」</title><link>https://prtimes.jp/a/2</link>
<dc:date>2026-11-01T09:00:00+09:00</dc:date></item>
</rdf:RDF>""".encode("utf-8")

RSS2 = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>映画『ゴジラ-0.0』より「二式大型飛行艇(改)」が登場</title><link>https://example.com/m/1</link>
<pubDate>Tue, 24 Nov 2026 08:00:00 +0900</pubDate></item></channel></rss>""".encode("utf-8")

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>「ことばずかん Super DX」予約開始</title><link href="https://example.com/e/1"/>
<updated>2026-11-24T10:00:00Z</updated></entry></feed>""".encode("utf-8")

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://prtimes.jp/main/html/rd/p/000000001.000005808.html</loc><news:news>
<news:publication_date>2026-11-24T19:00:00+09:00</news:publication_date>
<news:title>【新商品】「おしりにぎり」全4種が登場！</news:title>
<news:keywords>ぬいぐるみ,おもちゃ・遊具・人形</news:keywords></news:news></url>
<url><loc>https://prtimes.jp/main/html/rd/p/000000002.000000001.html</loc><news:news>
<news:publication_date>2026-11-24T19:10:00+09:00</news:publication_date>
<news:title>「光の花の庭」が日本一</news:title>
<news:keywords>旅行・観光,イルミネーション</news:keywords></news:news></url>
</urlset>""".encode("utf-8")


def test_parse_feed_rss1_rss2_atom():
    r1 = a.parse_feed(RSS1)
    assert [x["title"][:4] for x in r1] == ["知育玩具", "古い発表"]
    assert r1[0]["link"] == "https://prtimes.jp/a/1"
    assert r1[0]["date"] == datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    assert r1[0]["desc"] == "本文"
    r2 = a.parse_feed(RSS2)
    assert r2[0]["date"] == datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
    r3 = a.parse_feed(ATOM)
    assert r3[0]["link"] == "https://example.com/e/1"
    assert a.parse_feed(b"<not xml") == []


def test_parse_news_sitemap_filters_by_category():
    rows = a.parse_news_sitemap(SITEMAP, ["おもちゃ・遊具・人形"])
    assert len(rows) == 1
    assert rows[0]["link"].endswith("000005808.html")
    assert rows[0]["date"] == datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    assert len(a.parse_news_sitemap(SITEMAP, [])) == 2  # カテゴリ指定なしは全部


def test_extract_names_skips_tags_and_events():
    assert a.extract_names("【新商品】「おしりにぎり」全4種が登場！") == ["おしりにぎり"]
    assert a.extract_names("「むにゅぐるみパティオ POP UP」開催「ふわふわうさぎ」") == ["ふわふわうさぎ"]
    assert a.extract_names("「AB」だけ") == []  # 3 字未満
    assert a.extract_names("「ことばずかん™ Super DX」") == ["ことばずかん Super DX"]


def test_search_name_prefers_product_after_yori():
    t = "映画『ゴジラ-0.0』より「二式大型飛行艇(改)」が登場"
    assert a.search_name(t, a.extract_names(t)) == "二式大型飛行艇(改)"
    t2 = "「くるくるチャイム ミニ」と「チャイム用レール」を発売"
    assert a.search_name(t2, a.extract_names(t2)) == "くるくるチャイム ミニ"
    assert a.search_name("題だけ", []) is None


def _entry(feed, kind, title, hour, release=True):
    return {"feed": feed, "kind": kind, "title": title, "link": "", "release": release,
            "date": datetime(2026, 11, 24, hour, tzinfo=timezone.utc).isoformat(),
            "names": a.extract_names(title)}


def test_pick_targets_order_dedupe_and_cap():
    es = [
        _entry("hobbywatch", "media", "「フィギュアA」登場", 11),
        _entry("m", "maker", "「古い発売品」発売", 1),
        _entry("m", "maker", "「新しい発売品」発売", 9),
        _entry("m", "maker", "「発売の語なし」のお知らせ", 10, release=False),
        _entry("s", "sitemap", "「新しい発売品」発売", 8),  # 同じ商品名は 1 回だけ
    ]
    got = [n for _, n in a.pick_targets(es, 10)]
    assert got == ["新しい発売品", "古い発売品", "発売の語なし", "フィギュアA"]
    assert len(a.pick_targets(es, 2)) == 2


def test_match_score():
    assert a.match_score("くるくるチャイム ミニ", "ピープル くるくるチャイム ミニ 知育玩具") == 1.0
    assert a.match_score("くるくるチャイム ミニ", "別の商品 ブロック") == 0.0
    assert a.match_score("", "x") == 0.0


def _api_item(asin, title):
    # Creators API の実際の形 (searchResult.items・itemInfo.title.displayValue)。
    # probe_demand_supply の初版はトップレベルの items を見て全件 0 件になった (同じ罠を避ける)
    return {"asin": asin, "itemInfo": {"title": {"displayValue": title}},
            "offersV2": {"listings": [{"price": {"money": {"amount": 2980}},
                                       "merchantInfo": {"name": "Amazon.co.jp"}}]},
            "browseNodeInfo": {"browseNodes": [
                {"id": "1", "displayName": "知育玩具", "ancestor": {"displayName": "おもちゃ"}}]}}


class FakeAPI:
    def __init__(self):
        self.calls = []

    def search_items(self, **kw):
        self.calls.append(kw)
        return {"searchResult": {"items": [
            _api_item("B0ZZZZZZZ1", "関係ない商品"),
            _api_item("B0HZZZZZZ2", "ピープル くるくるチャイム ミニ 知育玩具"),
        ]}}


def _sources(tmp_path):
    p = tmp_path / "src.json"
    p.write_text(json.dumps({"feeds": [
        {"id": "maker", "kind": "maker", "url": "u1"},
        {"id": "broken", "kind": "maker", "url": "u2"},
        {"id": "sm", "kind": "sitemap", "url": "u3", "categories": ["おもちゃ・遊具・人形"]},
    ]}), encoding="utf-8")
    return p


def _fetch(url):
    if url == "u2":
        raise OSError("down")
    return {"u1": RSS1, "u3": SITEMAP}[url]


def test_run_end_to_end_with_fake_api(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "load_catalog", lambda: ({"B0HZZZZZZ2"}, set()))
    api = FakeAPI()
    rep = a.run(36, 5, sources_path=_sources(tmp_path), api=api, fetch=_fetch,
                sleeper=lambda s: None, now=NOW)
    assert [f["error"] is None for f in rep["feeds"]] == [True, False, True]
    assert rep["targets"] == ["おしりにぎり", "くるくるチャイム ミニ"]  # 新しい順。古い発表は since の外
    assert api.calls[0]["search_index"] == "Toys"
    best = rep["results"][1]["hits"][0]
    assert best["asin"] == "B0HZZZZZZ2" and best["score"] == 1.0
    assert best["in_catalog"] is True and best["has_article"] is False
    assert best["need_sites"] == 1  # B0H は新発売
    s = rep["summary"]
    assert s["searched"] == 2 and s["found"] == 1 and s["found_new_release"] == 1
    assert "発表起点の影運転" in a.step_summary(rep)


def test_dry_run_does_not_search(tmp_path):
    rep = a.run(36, 5, sources_path=_sources(tmp_path), api=None, fetch=_fetch,
                sleeper=lambda s: None, now=NOW, dry_run=True)
    assert rep["results"] == [] and len(rep["targets"]) == 2


def test_search_error_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "load_catalog", lambda: (set(), set()))

    class Boom:
        def search_items(self, **kw):
            raise RuntimeError("TooManyRequests")
    rep = a.run(36, 5, sources_path=_sources(tmp_path), api=Boom(), fetch=_fetch,
                sleeper=lambda s: None, now=NOW)
    assert rep["summary"]["search_errors"] == 2 and rep["summary"]["found"] == 0


def test_main_fails_when_all_feeds_fail(tmp_path, monkeypatch):
    p = tmp_path / "src.json"
    p.write_text(json.dumps({"feeds": [{"id": "x", "kind": "maker", "url": "u2"}]}), encoding="utf-8")
    monkeypatch.setattr(a, "SOURCES_PATH", p)
    monkeypatch.setattr(a, "fetch_feed", _fetch)
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run", "--out", str(tmp_path / "o.json")])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # run の既定引数は import 時に束縛されるので、main から呼ぶ run の fetch を差し替える
    orig = a.run
    monkeypatch.setattr(a, "run", lambda *ar, **kw: orig(*ar, fetch=_fetch, sleeper=lambda s: None, **kw))
    assert a.main() == 1
