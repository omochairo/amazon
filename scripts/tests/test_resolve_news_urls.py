"""候補供給 P1a: Google ニュースの転送 URL を元記事に解いて覚える (resolve_news_urls) と、
それを記事レーンの出典の候補に足す (build_jules_prompt) のテスト。ネットワークには出ない。"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_jules_prompt as bjp  # noqa: E402
import resolve_news_urls as rn  # noqa: E402
import score_per_asin_info as sc  # noqa: E402

TODAY = dt.date(2026, 11, 23)
G = "https://news.google.com/rss/articles/CBMi{}?oc=5"


def _news(d: Path, asin: str, urls, titles=None):
    p = d / asin
    p.mkdir(parents=True, exist_ok=True)
    items = [{"url": u, "title": (titles or {}).get(u, "")} for u in urls]
    (p / "news.json").write_text(json.dumps({"items": items}), encoding="utf-8")


# ---------------------------------------------------------------- resolve_news_urls

def test_parse_batchexecute_reads_garturlres():
    payload = json.dumps(["garturlres", "https://www.example.co.jp/news/1", 1])
    line = json.dumps([["wrb.fr", "Fbv4je", payload, None, None, None, "generic"]])
    assert rn.parse_batchexecute(")]}'\n\n123\n" + line + "\n") == "https://www.example.co.jp/news/1"
    assert rn.parse_batchexecute("nothing here") is None


def test_is_gnews():
    assert rn.is_gnews(G.format("abc"))
    assert rn.is_gnews("https://news.google.com/articles/CBMiX")
    assert not rn.is_gnews("https://prtimes.jp/main/html/rd/p/1.html")


def test_pick_targets_round_robin_skips_articled_and_known(tmp_path):
    d = tmp_path / "per_asin"
    _news(d, "B0AAAAAAA1", [G.format("a1"), G.format("a2")])
    _news(d, "B0HHHHHHH1", [G.format("h1"), G.format("h2"), G.format("h3")])
    _news(d, "B0ZZZZZZZ1", [G.format("z1")])  # 記事がある
    _news(d, "B0CCCCCCC1", ["https://direct.example.jp/x"])  # 転送 URL でない
    cache = {"urls": {G.format("h1"): {"status": "ok", "url": "https://x.jp/1", "at": "2026-11-20"}}, "day": {}}
    got = rn.pick_targets(cache, str(d), {"B0ZZZZZZZ1"}, TODAY, limit=10)
    # ASIN の降順に、商品ごとに 1 件ずつ (解いたことのある h1 は除く)
    assert got == [G.format("h2"), G.format("a1"), G.format("h3"), G.format("a2")]
    assert rn.pick_targets(cache, str(d), {"B0ZZZZZZZ1"}, TODAY, limit=1) == [G.format("h2")]


def test_pick_targets_retries_errors_after_7_days(tmp_path):
    d = tmp_path / "per_asin"
    _news(d, "B0AAAAAAA1", [G.format("a1"), G.format("a2"), G.format("a3")])
    cache = {"urls": {
        G.format("a1"): {"status": "error", "at": "2026-11-20"},  # 3 日前: まだ
        G.format("a2"): {"status": "error", "at": "2026-11-16"},  # 7 日前: 解き直す
        G.format("a3"): {"status": "none", "at": "2026-01-01"},   # 元の URL 無し: 解き直さない
    }, "day": {}}
    assert rn.pick_targets(cache, str(d), set(), TODAY, limit=10) == [G.format("a2")]


def test_run_daily_limit_across_runs():
    cache = {"urls": {}, "day": {}}
    targets = [G.format(i) for i in range(5)]
    st = rn.run(cache, targets, TODAY, daily_limit=3, interval=0,
                decode_fn=lambda u: "https://media.example.jp/" + u[-6:], sleep=lambda s: None)
    assert st["ok"] == 3 and cache["day"] == {"date": "2026-11-23", "count": 3, "blocked": False}
    st2 = rn.run(cache, targets[3:], TODAY, daily_limit=3, interval=0,
                 decode_fn=lambda u: "https://media.example.jp/x", sleep=lambda s: None)
    assert st2["tried"] == 0
    # 日付が変わると数え直す
    st3 = rn.run(cache, targets[3:], TODAY + dt.timedelta(days=1), daily_limit=3, interval=0,
                 decode_fn=lambda u: "https://media.example.jp/" + u[-6:], sleep=lambda s: None)
    assert st3["ok"] == 2 and cache["day"]["count"] == 2


def test_run_stops_on_429_for_the_day():
    cache = {"urls": {}, "day": {}}
    calls = []

    def dec(u):
        calls.append(u)
        if len(calls) == 2:
            raise rn.Blocked("429")
        return "https://media.example.jp/ok"

    st = rn.run(cache, [G.format(i) for i in range(5)], TODAY, 20, 0, decode_fn=dec, sleep=lambda s: None)
    assert st == {"tried": 1, "ok": 1, "none": 0, "error": 0, "blocked": True}
    assert len(calls) == 2 and G.format(1) not in cache["urls"]  # 止められた分は覚えない
    st2 = rn.run(cache, [G.format(3)], TODAY, 20, 0, decode_fn=dec, sleep=lambda s: None)
    assert st2["tried"] == 0 and len(calls) == 2


def test_run_records_none_error_and_self_domain():
    cache = {"urls": {}, "day": {}}
    res = {G.format(0): None, G.format(1): "https://navi.omcha.jp/posts/x/", G.format(2): RuntimeError("x")}

    def dec(u):
        r = res[u]
        if isinstance(r, Exception):
            raise r
        return r

    rn.run(cache, list(res), TODAY, 20, 0, decode_fn=dec, sleep=lambda s: None)
    assert cache["urls"][G.format(0)]["status"] == "none"
    assert cache["urls"][G.format(1)]["status"] == "none"  # 自社の記事は根拠にしない
    assert cache["urls"][G.format(2)] == {"status": "error", "at": "2026-11-23"}
    assert rn.resolved_url(cache, G.format(0)) is None


def test_main_writes_cache(tmp_path, monkeypatch):
    d = tmp_path / "per_asin"
    _news(d, "B0AAAAAAA1", [G.format("a1")])
    (tmp_path / "articles").mkdir()
    monkeypatch.setattr(rn, "decode", lambda u, **k: "https://media.example.jp/a1")
    cache = tmp_path / "news_resolved.json"
    rn.main(["--per-asin-dir", str(d), "--articles-dir", str(tmp_path / "articles"),
             "--cache", str(cache), "--interval", "0"])
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["urls"][G.format("a1")]["url"] == "https://media.example.jp/a1"
    assert data["day"]["count"] == 1


# ---------------------------------------------------------------- build_jules_prompt

ASIN = "B0TEST0002"
NEWS_A = "https://media-a.example.jp/news/1"
NEWS_B = "https://media-b.example.com/n/2"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    d = tmp_path / "data" / "raw" / "per_asin"
    urls = [G.format(i) for i in range(5)]
    _news(d, ASIN, urls, {G.format(0): "新商品の発表"})
    resolved = {
        G.format(0): {"status": "ok", "url": NEWS_A, "at": "2026-11-23"},
        G.format(1): {"status": "ok", "url": NEWS_B, "at": "2026-11-23"},
        G.format(2): {"status": "none", "at": "2026-11-23"},
        G.format(3): {"status": "ok", "url": "https://media-c.example.net/3", "at": "2026-11-23"},
        G.format(4): {"status": "ok", "url": "https://media-d.example.org/4", "at": "2026-11-23"},
    }
    (tmp_path / "data" / "raw" / "news_resolved.json").write_text(
        json.dumps({"urls": resolved, "day": {}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sc, "prior_article_urls", lambda asin, *a, **k: ())
    monkeypatch.setattr(sc, "first_party_posts", lambda asin, *a, **k: [])
    return tmp_path


def test_resolved_news_added_only_for_no_web(repo):
    plain = bjp.source_candidates(ASIN)
    assert plain["third"] == [] and not plain["news"]
    cand = bjp.source_candidates(ASIN, no_web=True)
    assert [u for u, _ in cand["third"]] == [NEWS_A, NEWS_B, "https://media-c.example.net/3"]  # 3 件まで
    assert cand["third"][0][1] == "ニュース: 新商品の発表"
    assert cand["news"] == {NEWS_A, NEWS_B, "https://media-c.example.net/3"}
    assert bjp.excerpt_targets(cand)[:2] == [NEWS_A, NEWS_B]


def test_news_candidate_needs_rare_word(repo, monkeypatch):
    monkeypatch.setattr(bjp, "rare_tokens", lambda asin: {"33506"} if asin == ASIN else set())
    monkeypatch.setattr(bjp, "identifies_product", lambda hits: True)
    cand = bjp.source_candidates(ASIN, no_web=True)
    ex = {
        NEWS_A: ("ok", "ブリオ 33506 を発売", ["33506", "ブリオ", "レールウェイ"], 200),
        NEWS_B: ("ok", "ブリオの新シリーズ", ["ブリオ", "レールウェイ", "木のおもちゃ"], 200),
    }
    assert bjp.citable_urls(cand, ex) == [NEWS_A]
    # 同じ一致でも、ニュース以外の候補には追加の条件を掛けない
    cand2 = dict(cand, news=set())
    assert bjp.citable_urls(cand2, ex) == [NEWS_A, NEWS_B]
    note = bjp._sources_note(ASIN, no_web=True, excerpts=ex)
    assert "ニュース検索の結果で、本文にこの商品に固有の名前・型番が無い" in note
    assert note.count("ニュース検索の結果で") == 1


def test_rare_tokens_uses_catalog_frequency(repo, monkeypatch):
    d = repo / "data" / "raw" / "per_asin"

    def item(asin, title):
        (d / asin).mkdir(parents=True, exist_ok=True)
        (d / asin / "amazon.json").write_text(json.dumps({"item": {"asin": asin, "title": title}}),
                                               encoding="utf-8")

    item(ASIN, "ブリオ レールウェイ 33506")
    for i in range(4):
        item(f"B0OTHER00{i}", f"ブリオ レールウェイ 9{i}000")
    (repo / "data" / "raw" / "amazon.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(bjp, "_catalog_df_cache", None)
    rare = bjp.rare_tokens(ASIN)
    assert "33506" in rare
    assert "ブリオ" not in rare and "レールウェイ" not in rare  # 5 商品に出る


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, get, post=None):
        self._get, self._post = get, post

    def get(self, *a, **k):
        return self._get

    def post(self, *a, **k):
        return self._post


def test_decode_with_fake_session():
    page = _Resp(200, '<div data-n-a-sg="SIG" data-n-a-ts="1700000000"></div>')
    payload = json.dumps(["garturlres", "https://www.example.co.jp/n/9", 1])
    body = _Resp(200, json.dumps([["wrb.fr", "Fbv4je", payload]]))
    assert rn.decode(G.format("x"), session=_Session(page, body)) == "https://www.example.co.jp/n/9"
    with pytest.raises(rn.Blocked):
        rn.decode(G.format("x"), session=_Session(_Resp(429)))
    with pytest.raises(rn.Blocked):
        rn.decode(G.format("x"), session=_Session(page, _Resp(429)))
    # 同意画面など署名の無いページは失敗 (7 日後に解き直す)。「元の URL 無し」にしない
    with pytest.raises(ValueError):
        rn.decode(G.format("x"), session=_Session(_Resp(200, "<html>consent</html>")))
    assert rn.decode("https://example.jp/not-gnews") is None
