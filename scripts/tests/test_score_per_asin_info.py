"""Unit tests for score_per_asin_info.score_asin (#1600 Phase 1).

band 判定の境界:
  - unfetched : news/youtube/books ファイル自体が無い (fetch 未実行) → defer しない
  - zero      : fetch 済みで第三者材料ゼロ かつ tier=D → defer 対象 (真ゼロ)
  - thin / ok : 材料の多寡で分岐
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import score_per_asin_info as S  # noqa: E402


def _write(d: pathlib.Path, name: str, obj) -> None:
    with open(d / name, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class ScorePerAsinInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk(self, asin: str) -> pathlib.Path:
        d = self.base / asin
        d.mkdir(parents=True)
        return d

    def test_unfetched_no_source_files(self):
        # amazon + competitors のみ (filter_raw_per_asin 未実行) → unfetched, defer 対象外
        d = self._mk("B00000DMD2")
        _write(d, "amazon.json", {"item": {"title": "謎ブランドのおもちゃ"}})
        _write(d, "competitors.json", {"competitors": [{"asin": "X"}] * 5})
        r = S.score_asin("B00000DMD2", self.base)
        self.assertEqual(r["band"], "unfetched")
        self.assertFalse(r["evidence_fetched"])
        self.assertEqual(r["evidence_score"], 0)

    def test_true_zero_fetched_empty_dtier(self):
        # fetch 済み (空ファイル) かつ D-tier → zero (真ゼロ, defer 対象)
        d = self._mk("B0FAKEZERO1")
        _write(d, "amazon.json", {"item": {"title": "Bajoy 知育マット"}})
        _write(d, "news.json", {"items": []})
        _write(d, "youtube.json", {"items": []})
        _write(d, "books.json", {"items": []})
        r = S.score_asin("B0FAKEZERO1", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertTrue(r["evidence_fetched"])
        self.assertEqual(r["evidence_score"], 0)
        self.assertEqual(r["brand_tier"], "D")

    def test_fetched_empty_but_known_brand_not_zero(self):
        # fetch 済みで材料ゼロでも S/A tier は google_search フォールバックが効く → zero にしない
        d = self._mk("B0KNOWNBRND")
        _write(d, "amazon.json", {"item": {"title": "レゴ クラシック 黄色のアイデアボックス"}})
        _write(d, "news.json", {"items": []})
        r = S.score_asin("B0KNOWNBRND", self.base)
        self.assertNotEqual(r["band"], "zero")
        self.assertEqual(r["brand_tier"], "S")

    def test_distinct_news_sources_from_title_suffix(self):
        # Google News RSS は url が固定リダイレクトのため title 末尾の媒体名で distinct 集計
        d = self._mk("B0NEWSRICH1")
        _write(d, "amazon.json", {"item": {"title": "謎ブランド おもちゃ"}})
        _write(d, "news.json", {"items": [
            {"title": "記事A - 朝日新聞"},
            {"title": "記事B - 朝日新聞"},          # 同媒体 → 重複しない
            {"title": "記事C - Rolling Stone Japan(ローリングストーン)"},  # (...) は畳む
            {"title": "媒体名なし見出し"},            # セパレータ無し → 無視
        ]})
        r = S.score_asin("B0NEWSRICH1", self.base)
        self.assertEqual(r["news_sources"], 2)

    def test_rich_asin_is_ok(self):
        d = self._mk("B0RICHASIN1")
        _write(d, "amazon.json", {"item": {"title": "謎ブランド 知育"}})
        _write(d, "news.json", {"items": [
            {"title": f"記事{i} - 媒体{i}"} for i in range(5)
        ]})
        _write(d, "youtube.json", {"items": [{"title": "v"}] * 4})
        r = S.score_asin("B0RICHASIN1", self.base)
        self.assertEqual(r["band"], "ok")
        self.assertGreaterEqual(r["evidence_score"], 40)

    def test_title_only_youtube_items_do_not_count(self):
        # #8163 レビュー指摘: _match: "title_only" (#8162 案A) は enrich/defer
        # 優先度の材料にしない。全件 title_only なら yt=0 と同じ扱い。
        d = self._mk("B0TITLEONLY")
        _write(d, "amazon.json", {"item": {"title": "謎ブランドのおもちゃ"}})
        _write(d, "news.json", {"items": []})
        _write(d, "youtube.json", {"items": [
            {"title": "v1", "_match": "title_only"},
            {"title": "v2", "_match": "title_only"},
        ]})
        _write(d, "books.json", {"items": []})
        r = S.score_asin("B0TITLEONLY", self.base)
        self.assertEqual(r["youtube"], 0)
        self.assertEqual(r["band"], "zero")


class ThirdPartySourcesBandTest(unittest.TestCase):
    """#5490 案B: third_party_sources.json を band 判定に配線した分の境界。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk_zero_asin(self, asin: str, sources: list | None = None) -> pathlib.Path:
        """band=zero になる素材 (fetch 済み・空・D tier) を作る。"""
        d = self.base / asin
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "Bajoy 知育マット"}})
        for name in ("news.json", "youtube.json", "books.json"):
            _write(d, name, {"items": []})
        if sources is not None:
            _write(d, "third_party_sources.json", {"sources": sources})
        return d

    def test_no_third_party_stays_zero(self):
        self._mk_zero_asin("B0TP000000")
        r = S.score_asin("B0TP000000", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertEqual(r["third_party_hosts"], 0)

    def test_one_host_stays_zero(self):
        # §6.5.1 は非販売 2 件必須。1 件では zero を外さない。
        self._mk_zero_asin("B0TP000001", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
        ])
        r = S.score_asin("B0TP000001", self.base)
        self.assertEqual(r["band"], "zero")
        self.assertEqual(r["third_party_hosts"], 1)

    def test_two_hosts_escape_zero_to_thin(self):
        self._mk_zero_asin("B0TP000002", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://mokutopia.com/products/x", "host": "mokutopia.com"},
        ])
        r = S.score_asin("B0TP000002", self.base)
        self.assertEqual(r["band"], "thin")
        self.assertEqual(r["third_party_hosts"], 2)
        # evidence は動かさない (thin/ok の境界を third_party で越えさせない)
        self.assertEqual(r["evidence_score"], 0)

    def test_same_host_twice_is_one(self):
        self._mk_zero_asin("B0TP000003", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://www.note.com/a/n/2"},  # host 欠落 + www → 同一に畳む
        ])
        r = S.score_asin("B0TP000003", self.base)
        self.assertEqual(r["third_party_hosts"], 1)
        self.assertEqual(r["band"], "zero")

    def test_search_result_pages_do_not_count(self):
        # 既に書かれた JSON には search.kakaku.com が 409 件残っている (2026-08-18 実測)。
        # fetch 側を直しても過去分は残るので、採点側でも落ちること。
        self._mk_zero_asin("B0TP000004", [
            {"url": "https://search.kakaku.com/gravitrax", "host": "search.kakaku.com"},
            {"url": "https://www.yamada-denkiweb.com/search/x", "host": "yamada-denkiweb.com"},
            {"url": "https://www.biccamera.com/bc/category?q=x", "host": "biccamera.com"},
        ])
        r = S.score_asin("B0TP000004", self.base)
        self.assertEqual(r["third_party_hosts"], 0)
        self.assertEqual(r["band"], "zero")

    def test_third_party_alone_never_reaches_ok(self):
        # host を上限まで積んでも evidence は 0 のままなので ok にはならない。
        self._mk_zero_asin("B0TP000005", [
            {"url": f"https://ex{i}.com/a", "host": f"ex{i}.com"} for i in range(8)
        ])
        r = S.score_asin("B0TP000005", self.base)
        self.assertEqual(r["band"], "thin")
        self.assertEqual(r["third_party_hosts"], 8)
        self.assertEqual(r["info_score"], 4 * 3 + 4)  # third_party 上限 12 + tier D 4

    def test_unfetched_is_unchanged_by_third_party(self):
        # news/youtube/books が未収集なら third_party があっても enrich 待ち (defer 対象外)。
        d = self.base / "B0TP000006"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "謎ブランドのおもちゃ"}})
        _write(d, "third_party_sources.json", {"sources": [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://mokutopia.com/products/x", "host": "mokutopia.com"},
        ]})
        r = S.score_asin("B0TP000006", self.base)
        self.assertEqual(r["band"], "unfetched")

    def test_known_brand_zero_evidence_still_thin(self):
        # tier が D でなければ third_party の有無に関係なく従来どおり thin。
        d = self.base / "B0TP000007"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "レゴ クラシック アイデアボックス"}})
        for name in ("news.json", "youtube.json", "books.json"):
            _write(d, name, {"items": []})
        r = S.score_asin("B0TP000007", self.base)
        self.assertEqual(r["band"], "thin")

    def test_malformed_third_party_file_is_ignored(self):
        self._mk_zero_asin("B0TP000008")
        (self.base / "B0TP000008" / "third_party_sources.json").write_text(
            "{ not json", encoding="utf-8")
        r = S.score_asin("B0TP000008", self.base)
        self.assertEqual(r["third_party_hosts"], 0)
        self.assertEqual(r["band"], "zero")


class ShouldDeferTest(unittest.TestCase):
    """03-invoke-jules の入口除外。ranking_pool の unfetched 品が素材ゼロで pick される穴。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk_unfetched(self, asin: str, sources: list | None = None) -> None:
        # ranking_pool 品の実態: amazon.json と competitors.json だけ (S tier でも同じ)
        d = self.base / asin
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "バンダイ ロボットおもちゃ"}})
        _write(d, "competitors.json", {"competitors": [{"asin": f"B0CP00000{i}"} for i in range(5)]})
        if sources is not None:
            _write(d, "third_party_sources.json", {"sources": sources})

    def test_unfetched_without_third_party_is_deferred(self):
        self._mk_unfetched("B0SD000001")
        r = S.score_asin("B0SD000001", self.base)
        self.assertEqual(r["band"], "unfetched")
        self.assertTrue(S.should_defer(r))

    def test_unfetched_with_one_host_is_deferred(self):
        self._mk_unfetched("B0SD000002", [{"url": "https://note.com/a/n/1", "host": "note.com"}])
        self.assertTrue(S.should_defer(S.score_asin("B0SD000002", self.base)))

    def test_unfetched_with_two_sites_is_picked(self):
        # navi-brain#92: 合計 5 件の見込みは見ない。非販売 2 サイトで生成に回す
        self._mk_unfetched("B0SD000003", [
            {"url": "https://note.com/a/n/1", "host": "note.com"},
            {"url": "https://mokutopia.com/products/x", "host": "mokutopia.com"},
        ])
        r = S.score_asin("B0SD000003", self.base)
        self.assertEqual(r["band"], "unfetched")
        self.assertEqual(r["third_party_sites"], 2)
        self.assertEqual(S.non_sales_material(r), 2)
        self.assertFalse(S.should_defer(r))

    def test_same_site_hosts_count_once(self):
        # navi-brain#92: gate と同じくサイトで数える (ja/en の wikipedia、SNS 同士は 1)
        self._mk_unfetched("B0SD000006", [
            {"url": "https://ja.wikipedia.org/wiki/x", "host": "ja.wikipedia.org"},
            {"url": "https://en.wikipedia.org/wiki/x", "host": "en.wikipedia.org"},
            {"url": "https://www.youtube.com/watch?v=1", "host": "youtube.com"},
            {"url": "https://www.instagram.com/p/1/", "host": "instagram.com"},
        ])
        r = S.score_asin("B0SD000006", self.base)
        self.assertEqual(r["third_party_hosts"], 4)
        self.assertEqual(r["third_party_sites"], 2)
        self.assertEqual(S.non_sales_material(r), 2)

    def test_one_site_with_two_hosts_waits(self):
        self._mk_unfetched("B0SD000010", [
            {"url": "https://toy.bandai.co.jp/x", "host": "toy.bandai.co.jp"},
            {"url": "https://www.bandai.co.jp/y", "host": "bandai.co.jp"},
        ])
        r = S.score_asin("B0SD000010", self.base)
        self.assertEqual(r["third_party_sites"], 1)
        self.assertTrue(S.should_defer(r))

    def test_retail_and_unrelated_hosts_are_not_non_sales(self):
        # #9239 (#9193 / #9211): 通販サイトと会社情報は「非販売 2 件」に数えない
        self._mk_unfetched("B0SD000007", [
            {"url": "https://www.monotaro.com/p/1/", "host": "monotaro.com"},
            {"url": "https://www.askul.co.jp/p/1/", "host": "askul.co.jp"},
            {"url": "https://rocketreach.co/x", "host": "rocketreach.co"},
            {"url": "https://find-and-update.company-information.service.gov.uk/company/1"},
            {"url": "https://hk.finance.yahoo.com/quote/x", "host": "hk.finance.yahoo.com"},
        ])
        r = S.score_asin("B0SD000007", self.base)
        self.assertEqual(r["third_party_hosts"], 0)
        self.assertEqual(r["retail_hosts"], 2)
        self.assertEqual(r["unrelated_hosts"], 3)
        self.assertTrue(S.should_defer(r))

    def test_mall_pages_counts_matched_rakuten_and_yahoo(self):
        root = self.base / "raw"
        base = root / "per_asin"
        base.mkdir(parents=True)
        _write(root, "rakuten_matched.json", {"items": [
            {"matched_asin": "B0SD000008", "url": "https://hb.afl.rakuten.co.jp/x"},
            {"matched_asin": "B0SD000009", "url": ""},  # URL の無い行は数えない
        ]})
        _write(root, "yahoo_matched.json", {"items": [
            {"matched_asin": "B0SD000008", "url": "https://ck.jp.ap.valuecommerce.com/x"},
            # 照合の誤り: 出品 URL に別の ASIN が埋まっている (B0HCTDR9ZN の実例)
            {"matched_asin": "B0SD000009", "url": "https://ck.jp.ap.valuecommerce.com/servlet/"
             "referral?vc_url=https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Fx%2Fs-b0bxslrtpj-1.html"},
        ]})
        self.assertEqual(S.mall_pages("B0SD000008", root), 3)
        self.assertEqual(S.mall_pages("B0SD000009", root), 1)

    def test_other_asin_in_url(self):
        self.assertTrue(S.other_asin_in_url(
            "https://x/?vc_url=https%3A%2F%2Fs.jp%2Fs-b0bxslrtpj-1.html", "B0HCTDR9ZN"))
        self.assertFalse(S.other_asin_in_url("https://s.jp/s-b0hctdr9zn-1.html", "B0HCTDR9ZN"))
        self.assertFalse(S.other_asin_in_url("https://item.rakuten.co.jp/shop/123/", "B0HCTDR9ZN"))
        # ハッシュ等の途中にある b0 から始まる文字列は ASIN とみなさない
        self.assertFalse(S.other_asin_in_url("https://hb.afl/hgc/xb0abcdefgh1/", "B0HCTDR9ZN"))

    def test_mall_urls_left_in_old_json_are_not_third_party(self):
        self.assertEqual(S.host_kind("www.amazon.co.jp"), "retail")
        self.assertEqual(S.host_kind("store.shopping.yahoo.co.jp"), "retail")
        self.assertEqual(S.host_kind("item.rakuten.co.jp"), "retail")

    def test_hand_built_result_without_sites_uses_hosts(self):
        # 手で組んだ結果 (third_party_sites 無し) は host 数で代用する
        r = {"band": "thin", "third_party_hosts": 2}
        self.assertEqual(S.non_sales_material(r), 2)
        self.assertFalse(S.should_defer(r))

    def test_host_kind_matches_subdomains_not_substrings(self):
        self.assertEqual(S.host_kind("www.yodobashi.com"), "retail")
        self.assertEqual(S.host_kind("hk.finance.yahoo.com"), "unrelated")
        self.assertEqual(S.host_kind("notyodobashi.com"), "third_party")
        self.assertEqual(S.host_kind("note.com"), "third_party")

    def test_zero_is_deferred_and_ok_is_not(self):
        self.assertTrue(S.should_defer({"band": "zero", "third_party_hosts": 0}))
        self.assertFalse(S.should_defer({"band": "ok", "third_party_hosts": 0}))

    def test_thin_waits_until_two_non_sales_sources(self):
        # #9199: thin でも非販売の材料 (第三者 host + news の媒体) が 2 件未満なら待たせる
        self.assertTrue(S.should_defer({"band": "thin", "third_party_hosts": 0}))
        self.assertTrue(S.should_defer({"band": "thin", "third_party_hosts": 1}))
        self.assertFalse(S.should_defer({"band": "thin", "third_party_hosts": 2}))
        self.assertFalse(S.should_defer(
            {"band": "thin", "third_party_hosts": 1, "news_sources": 1}))
        # navi-brain#92: news は Google ニュースの転送 URL (news.google.com) で出典に入り、
        # gate では媒体が違っても google.com の 1 サイトになる。news だけでは揃わない
        self.assertTrue(S.should_defer(
            {"band": "thin", "third_party_hosts": 0, "news_sources": 2}))

    def test_sources_exhausted_only_after_collection(self):
        # 収集前の待ちは「待てば揃う」。収集後も足りないものだけ打ち切り扱い
        waiting = {"band": "thin", "third_party_hosts": 0, "third_party_fetched": False}
        self.assertFalse(S.sources_exhausted(waiting))
        self.assertTrue(S.sources_exhausted(dict(waiting, third_party_fetched=True)))
        self.assertTrue(S.sources_exhausted(
            {"band": "zero", "third_party_hosts": 1, "third_party_fetched": True}))
        self.assertFalse(S.sources_exhausted(
            {"band": "thin", "third_party_hosts": 2, "third_party_fetched": True}))

    def test_score_reports_whether_collection_ran(self):
        self._mk_unfetched("B0SD000004", [])
        self.assertTrue(S.score_asin("B0SD000004", self.base)["third_party_fetched"])
        self.assertFalse(S.score_asin("B0SD000099", self.base)["third_party_fetched"])


class FirstPartyPostTest(unittest.TestCase):
    """#9199 案(b): omcha.jp の実使用記事を非販売ソース 1 件として数える。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fps = pathlib.Path(self._tmp.name) / "first_party_sources.json"
        _write(self.fps.parent, self.fps.name, {"sources": [
            {"asin": "B0FP000001", "role": "primary", "post_url": "https://omcha.jp/a/"},
            {"asin": "B0FP000001", "role": "primary", "post_url": "https://omcha.jp/b/"},
            {"asin": "B0FP000002", "role": "compared", "post_url": "https://omcha.jp/c/"},
            {"asin": "B0FP000003", "role": "primary",
             "post_url": "https://navi.omcha.jp/products/b0fp000003/"},
        ]})

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_primary_posts_on_omcha_jp(self):
        self.assertEqual(S.first_party_posts("B0FP000001", self.fps),
                         ("https://omcha.jp/a/", "https://omcha.jp/b/"))
        self.assertEqual(S.first_party_posts("B0FP000002", self.fps), ())  # compared
        self.assertEqual(S.first_party_posts("B0FP000003", self.fps), ())  # navi 自身
        self.assertEqual(S.first_party_posts("B0FP000009", self.fps), ())

    def test_missing_file_is_empty(self):
        self.assertEqual(S.first_party_posts("B0FP000001", self.fps.parent / "nope.json"), ())

    def test_counts_as_one_non_sales_source_at_most(self):
        r = {"band": "thin", "third_party_hosts": 1, "first_party_posts": 2}
        self.assertEqual(S.non_sales_material(r), 2)
        self.assertFalse(S.should_defer(r))
        # 第三者 0 件なら omcha.jp だけでは揃わない (2 件目は外部から要る)
        self.assertTrue(S.should_defer({"band": "thin", "third_party_hosts": 0,
                                        "first_party_posts": 2}))


class IsSearchResultUrlTest(unittest.TestCase):
    def test_search_pages(self):
        for u in (
            "https://search.kakaku.com/gravitrax",
            "https://www.yamada-denkiweb.com/search/%E3%82%A2",
            "https://www.biccamera.com/bc/category?q=x",
            "https://giftmall.co.jp/search/x",
            "https://example.com/x?keyword=y",
        ):
            self.assertTrue(S.is_search_result_url(u), u)

    def test_editorial_pages_kept(self):
        for u in (
            "https://note.com/monte/n/n146db59af44e",
            "https://mokutopia.com/products/rocket-puzzle-box",
            "https://review.kakaku.com/review/K0001/",
            "https://ameblo.jp/x/entry-12855648969.html",
            "https://research-toys.example.jp/report",
        ):
            self.assertFalse(S.is_search_result_url(u), u)

    def test_garbage_is_not_counted(self):
        for u in ("", "not a url", "ftp:///"):
            self.assertTrue(S.is_search_result_url(u), u)


if __name__ == "__main__":
    unittest.main()


class PriorArticleSitesTest(unittest.TestCase):
    """navi-brain#92: 書き直しでは、前の記事の非販売の出典も材料に数える。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.base = root / "per_asin"
        self.arts = root / "articles"
        self.arts.mkdir()
        self._orig = S.ARTICLES_DIR
        S.ARTICLES_DIR = self.arts
        S._article_index.cache_clear()

    def tearDown(self) -> None:
        S.ARTICLES_DIR = self._orig
        S._article_index.cache_clear()
        self._tmp.cleanup()

    def _article(self, slug, urls):
        _write(self.arts, f"{slug}.json", {"slug": slug, "sources": [
            {"id": f"s{i}", "url": u} for i, u in enumerate(urls)]})

    def test_prior_urls_skip_sales_retail_self_and_take_latest(self):
        self._article("2026-06-01-B0PA000001", ["https://old.example/a"])
        self._article("2026-08-01-B0PA000001", [
            "https://www.amazon.co.jp/dp/B0PA000001/",
            "https://ck.jp.ap.valuecommerce.com/servlet/referral?x=1",
            "https://www.yodobashi.com/product/1/",
            "https://omcha.jp/post/1/",
            "https://www.google.com/search?q=x",
            "https://example.org/review",
            "https://blog.example.net/b",
        ])
        self.assertEqual(S.prior_article_urls("B0PA000001"),
                         ("https://example.org/review", "https://blog.example.net/b"))
        self.assertEqual(S.prior_article_urls("B0PA000009"), ())

    def test_prior_sites_add_only_sites_not_in_candidates(self):
        d = self.base / "B0PA000002"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "テスト"}})
        _write(d, "third_party_sources.json", {"sources": [
            {"url": "https://example.org/x", "host": "example.org"}]})
        self._article("2026-08-01-B0PA000002", [
            "https://ja.example.org/y",            # 候補と同じサイト → 足さない
            "https://news.google.com/rss/articles/z",  # news として別に数える
            "https://example.net/b",
        ])
        r = S.score_asin("B0PA000002", self.base)
        self.assertEqual(r["third_party_sites"], 1)
        self.assertEqual(r["prior_article_sites"], 1)
        self.assertEqual(S.non_sales_material(r), 2)

    def test_prior_sites_lift_zero_band(self):
        # 素材ゼロ (zero) の判定も前の記事の出典を見る。見ないと書き直しが永久に待つ
        d = self.base / "B0PA000003"
        d.mkdir(parents=True)
        _write(d, "amazon.json", {"item": {"title": "無名 テスト"}})
        _write(d, "news.json", [])
        _write(d, "youtube.json", [])
        _write(d, "books.json", [])
        self.assertEqual(S.score_asin("B0PA000003", self.base)["band"], "zero")
        self._article("2026-08-01-B0PA000003", ["https://a.example/x", "https://b.example/y"])
        S._article_index.cache_clear()
        r = S.score_asin("B0PA000003", self.base)
        self.assertNotEqual(r["band"], "zero")
        self.assertFalse(S.should_defer(r))
