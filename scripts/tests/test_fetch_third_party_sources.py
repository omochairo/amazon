"""Unit tests for fetch_third_party_sources (#1600 Phase 2).

ネットワークを使わない純ロジック (host 抽出 / 除外判定 / source 整形 / freshness) を検証。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
import pathlib
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_third_party_sources as F  # noqa: E402


class HostFilterTest(unittest.TestCase):
    def test_host_strips_www(self):
        self.assertEqual(F._host("https://www.example.com/path"), "example.com")
        self.assertEqual(F._host("https://blog.example.jp/x"), "blog.example.jp")

    def test_host_keeps_leading_w_of_the_domain(self):
        # lstrip("www.") は文字集合を削るので "w"/"." 始まりの host の先頭を食う。
        # 実データで walmart.com が almart.com として保存され、配信物の
        # source_highlights に出典として表示されていた (2026-08-20 実測 10 ページ)。
        self.assertEqual(F._host("https://www.walmart.com/ip/x"), "walmart.com")
        self.assertEqual(F._host("https://walmart.com/ip/x"), "walmart.com")
        self.assertEqual(F._host("https://watch.impress.co.jp/x"), "watch.impress.co.jp")
        self.assertEqual(F._host("https://wish.com/x"), "wish.com")
        self.assertEqual(F._host("https://wiki.example.com/x"), "wiki.example.com")

    def test_host_strips_only_one_www_prefix(self):
        self.assertEqual(F._host("https://www.www.example.com/x"), "www.example.com")

    def test_retail_excluded(self):
        for u in (
            "https://www.amazon.co.jp/dp/B0XXXX",
            "https://item.rakuten.co.jp/shop/abc/",
            "https://store.shopping.yahoo.co.jp/x",
            "https://jp.mercari.com/item/m123",
        ):
            self.assertTrue(F._is_excluded(u), u)

    def test_search_engine_and_own_site_excluded(self):
        self.assertTrue(F._is_excluded("https://www.google.com/search?q=x"))
        self.assertTrue(F._is_excluded("https://navi.omcha.jp/posts/foo/"))

    def test_site_count_merges_same_site_and_sns(self):
        # navi-brain#92: 空振りの判定もサイトで数える (ja/en の wikipedia・SNS 同士は 1)
        self.assertEqual(F._site_count([
            {"url": "https://ja.wikipedia.org/wiki/x", "host": "ja.wikipedia.org"},
            {"url": "https://en.wikipedia.org/wiki/x", "host": "en.wikipedia.org"},
            {"url": "https://www.youtube.com/watch?v=1"},
            {"url": "https://x.com/a/status/1", "host": "x.com"},
        ]), 2)
        self.assertEqual(F._empty_streak({"sources": [
            {"url": "https://toy.bandai.co.jp/a"}, {"url": "https://www.bandai.co.jp/b"},
        ]}), 1)

    def test_unrelated_hosts_and_retail_sites_excluded(self):
        # #9239: 会社情報・求人・金融は商品と無関係なので取らない。
        # navi-brain#92: 通販サイトも取らない (合計 5 件の条件が無くなり、非販売にも数えない)
        for u in (
            "https://rocketreach.co/acme-profile",
            "https://find-and-update.company-information.service.gov.uk/company/1",
            "https://www.indeed.com/cmp/acme",
            "https://hk.finance.yahoo.com/quote/1234.T",
        ):
            self.assertTrue(F._is_excluded(u), u)
        for u in (
            "https://www.yodobashi.com/product/100000001001234567/",
            "https://www.monotaro.com/p/1234/",
        ):
            self.assertEqual(F._exclude_reason(u), "retail", u)

    def test_editorial_kept(self):
        for u in (
            "https://review.kakaku.com/review/K0001/",
            "https://mybest.example/best-toys",
            "https://www.itmedia.co.jp/news/article.html",
        ):
            self.assertFalse(F._is_excluded(u), u)

    def test_price_comparison_search_pages_excluded(self):
        # #5490 案B: 汎用エンジンだけを列挙していたため、価格比較/EC の検索 URL が
        # 素通りしていた (実測 2026-08-18: 収集済み 6,577 URL 中 search.kakaku.com が
        # 409 件で全 host 中 3 位)。検索語を URL に埋めただけのページは出典ではない。
        for u in (
            "https://search.kakaku.com/gravitrax",
            "https://www.yamada-denkiweb.com/search/%E3%82%A2%E3%82%AC%E3%83%84",
            "https://www.biccamera.com/bc/category?q=x",
            "https://giftmall.co.jp/search/x",
        ):
            self.assertTrue(F._is_excluded(u), u)

    def test_review_pages_of_same_sites_kept(self):
        # 検索 URL は落とすが、レビュー本体は第三者ソースとして残す。
        for u in (
            "https://review.kakaku.com/review/K0001234/",
            "https://mokutopia.com/products/rocket-puzzle-box",
        ):
            self.assertFalse(F._is_excluded(u), u)

    def test_non_http_excluded(self):
        self.assertTrue(F._is_excluded("ftp://example.com/x"))
        self.assertTrue(F._is_excluded(""))


class FilterSourcesTest(unittest.TestCase):
    def test_dedupe_by_host_and_cap(self):
        raw = [
            {"link": "https://a.example.com/1", "title": "A1", "snippet": "s"},
            {"link": "https://a.example.com/2", "title": "A2", "snippet": "s"},  # 同 host
            {"link": "https://www.amazon.co.jp/dp/X", "title": "buy"},           # retail 除外
            {"link": "https://b.example.com/x", "title": "B", "snippet": "t"},
            {"link": "https://c.example.com/y", "title": "C"},
        ]
        out = F._filter_sources(raw, max_sources=2)
        self.assertEqual(len(out), 2)
        self.assertEqual([s["host"] for s in out], ["a.example.com", "b.example.com"])
        self.assertEqual(out[0]["title"], "A1")

    def test_drop_reasons_recorded(self):
        # #9199: raw から候補に残らなかった分の理由を残す
        raw = [
            {"link": "https://a.example.com/1"},
            {"link": "https://a.example.com/2"},
            {"link": "https://www.amazon.co.jp/dp/X"},
            {"link": "https://www.google.com/search?q=x"},
            {"link": "https://navi.omcha.jp/posts/foo/"},
            {"link": "https://rocketreach.co/acme-profile"},
            {"link": "ftp://example.com/x"},
        ]
        out, dropped = F._filter_sources_with_drops(raw, max_sources=8)
        self.assertEqual([s["host"] for s in out], ["a.example.com"])
        self.assertEqual(
            [(d["host"], d["reason"]) for d in dropped],
            [
                ("a.example.com", "duplicate_host"),
                ("amazon.co.jp", "retail"),
                ("google.com", "search_result"),
                ("navi.omcha.jp", "self_domain"),
                ("rocketreach.co", "unrelated"),
                ("example.com", "not_http"),
            ],
        )
        self.assertEqual(len(out) + len(dropped), len(raw))

    def test_fetch_for_asin_writes_dropped(self):
        raw = [
            {"link": "https://a.example.com/1", "title": "積み木で遊んだ感想"},
            {"link": "https://item.rakuten.co.jp/shop/abc/", "title": "R"},
            {"link": "https://b.example.com/x", "title": "水資源の現況"},
        ]
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            (base / "B0TEST0001").mkdir()
            (base / "B0TEST0001" / "amazon.json").write_text(
                json.dumps({"title": "テスト 積み木"}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(F, "tavily_search", return_value=raw):
                F.fetch_for_asin("B0TEST0001", "tvly-test", base)
            saved = json.loads((base / "B0TEST0001" / F.OUT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["raw_count"], 3)
        self.assertEqual([x["host"] for x in saved["sources"]], ["a.example.com"])
        self.assertEqual(saved["dropped"], [
            {"host": "item.rakuten.co.jp", "reason": "retail"},
            {"host": "b.example.com", "reason": "off_topic"},
        ])

    def test_contact_pages_are_unrelated(self):
        # #9199: 商品名の検索で拾う無関係サイトの問い合わせ窓口
        self.assertEqual(F._exclude_reason("https://www.sophia.ac.jp/jpn/contact"), "unrelated")
        self.assertEqual(F._exclude_reason("https://example.jp/wp/contact-us/"), "unrelated")
        self.assertEqual(F._exclude_reason("https://example.jp/otoiawase.html"), "unrelated")
        # 語の一部に contact を含むだけのものは外さない
        self.assertIsNone(F._exclude_reason("https://example.jp/contacts-lens-review"))
        self.assertIsNone(F._exclude_reason("https://example.jp/blog/?p=contact"))
        self.assertIsNone(F._exclude_reason("https://example.com/inquiry-based-learning-toys"))


class RelevanceTest(unittest.TestCase):
    """#9199: クエリの語がタイトルにも本文にも出てこない結果は外す。"""

    def test_unrelated_page_is_off_topic(self):
        q = "サンリオ ぬいぐるみおせわセット シナモロール 199249"
        self.assertFalse(F._is_relevant(q, "お問い合わせ｜上智大学", "受付時間 9:00-17:00"))
        self.assertTrue(F._is_relevant(q, "シナモロールのおせわセットを買ってみた", ""))

    def test_matches_in_snippet(self):
        self.assertTrue(F._is_relevant("LAMPTOP 収納ボックス", "ブログ", "lamptop の箱を使ってみた"))

    def test_kana_and_spacing_are_folded(self):
        self.assertTrue(F._is_relevant("Original Tamagotchi 初代 すけるとん",
                                       "たまごっち スケルトンのレビュー", ""))
        self.assertTrue(F._is_relevant(
            "池田工業社 わくわくミッション宇宙探査セット［ スペースシャトル",
            "池田工業社 わくわくミッション 宇宙探査セット", ""))

    def test_long_token_partial_match(self):
        q = "ボーネルンドオリジナル ファーストピックアップパズル HY7"
        self.assertTrue(F._is_relevant(q, "1歳向けパズル ボーネルンド「ピックアップパズル」", ""))

    def test_token_length_is_measured_before_folding(self):
        # 「ゲーム」は長音を畳むと 2 文字になるが、照合には使う
        self.assertTrue(F._is_relevant("ゲーム", "人気のゲームを紹介", ""))

    def test_nakaguro_splits_tokens(self):
        q = "マイファースト・テディーメモリー"
        self.assertTrue(F._is_relevant(q, "テディーメモリーで遊んだ", ""))

    def test_short_tokens_are_not_used(self):
        # 2 文字以下の語 (「水」「木製」) では関連とみなさない
        self.assertFalse(F._is_relevant("木製 水 3D魔法ペイント", "水資源の現況 木製の橋", ""))

    def test_query_without_usable_tokens_keeps_everything(self):
        self.assertTrue(F._is_relevant("水 木", "何でも", ""))
        self.assertTrue(F._is_relevant("", "何でも", ""))

    def test_filter_without_query_skips_relevance(self):
        raw = [{"link": "https://a.example.com/1", "title": "無関係"}]
        self.assertEqual(len(F._filter_sources(raw, max_sources=5)), 1)
        out, dropped = F._filter_sources_with_drops(raw, 5, query="シナモロール")
        self.assertEqual(out, [])
        self.assertEqual(dropped, [{"host": "a.example.com", "reason": "off_topic"}])


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = pathlib.Path(self._tmp.name) / "tp.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, ts: str):
        self.p.write_text(json.dumps({"fetched_at": ts}), encoding="utf-8")

    def test_recent_is_fresh(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write(now)
        self.assertTrue(F._is_fresh(self.p, 30))

    def test_old_is_stale(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
        self._write(old)
        self.assertFalse(F._is_fresh(self.p, 30))

    def test_missing_is_stale(self):
        self.assertFalse(F._is_fresh(self.p, 30))


class TavilySearchTest(unittest.TestCase):
    def test_results_normalized_to_cse_shape(self):
        payload = {
            "results": [
                {"url": "https://a.example.com/1", "title": "A", "content": "snip a"},
                {"url": "https://b.example.com/2", "title": "B", "content": "snip b"},
                "not-a-dict",  # 異物は無視
            ]
        }
        resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
        resp.__enter__ = lambda *a: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        with mock.patch.object(F.urllib.request, "urlopen", return_value=resp) as urlopen:
            items = F.tavily_search("レゴ クラシック", "tvly-test", num=10)
        body = json.loads(urlopen.call_args.args[0].data)
        # #9199: exclude_domains を渡すと raw が 0 件近くまで減る (実測) ので渡さない
        self.assertNotIn("exclude_domains", body)
        self.assertEqual(body["max_results"], 10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], {
            "link": "https://a.example.com/1", "title": "A", "snippet": "snip a",
        })
        # _filter_sources に素通しできる shape であること
        out = F._filter_sources(items, max_sources=5)
        self.assertEqual([s["host"] for s in out], ["a.example.com", "b.example.com"])

    def test_missing_results_key_yields_empty(self):
        resp = io.BytesIO(json.dumps({}).encode("utf-8"))
        resp.__enter__ = lambda *a: resp  # type: ignore[attr-defined]
        resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
        with mock.patch.object(F.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(F.tavily_search("x", "tvly-test"), [])


class GscDemandPoolTest(unittest.TestCase):
    """#5490 案B / brain#13 2-3: 母集合を GSC 需要へ差し替えるレーン。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.base = self.root / "per_asin"
        self.hist = self.root / "gsc_by_page.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def _hist(self, rows):
        with open(self.hist, "w", encoding="utf-8") as f:
            for r in rows:
                print(json.dumps(r), file=f)

    def _asin(self, asin, tp_hosts=0):
        d = self.base / asin
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "amazon.json", "w", encoding="utf-8") as f:
            json.dump({"item": {"asin": asin, "title": f"{asin} テスト商品"}}, f)
        if tp_hosts:
            # 別々のサイト (navi-brain#92: 同じ登録ドメインの host は 1 サイトに数える)
            srcs = [{"url": f"https://h{i}.example/a", "host": f"h{i}.example"}
                    for i in range(tp_hosts)]
            with open(d / F.OUT_NAME, "w", encoding="utf-8") as f:
                json.dump({"asin": asin, "sources": srcs}, f)

    def test_window_anchors_on_latest_data_date_not_today(self):
        # GSC は数日遅れて届く。今日を右端にすると窓が空になる。
        self._hist([
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 12},
            {"date": "2026-05-01", "page": "https://navi.omcha.jp/products/b0aaaaaaa2/",
             "impressions": 99},
        ])
        imps = F._gsc_page_impressions(self.hist, days=28)
        self.assertEqual(imps, {"B0AAAAAAA1": 12})

    def test_impressions_summed_per_asin_and_non_product_pages_ignored(self):
        self._hist([
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 6},
            {"date": "2026-08-15", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 5},
            {"date": "2026-08-15", "page": "https://navi.omcha.jp/brands/lego/",
             "impressions": 500},
            {"date": "2026-08-15", "page": "https://navi.omcha.jp/", "impressions": 500},
        ])
        self.assertEqual(F._gsc_page_impressions(self.hist, days=28), {"B0AAAAAAA1": 11})

    def test_pool_is_imp_ranked_and_threshold_applied(self):
        self._hist([
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 10},
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa2/",
             "impressions": 40},
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa3/",
             "impressions": 9},  # しきい値未満
        ])
        for a in ("B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"):
            self._asin(a)
        got = F._gsc_demand_pool(self.base, history=self.hist, days=28,
                                 min_impressions=10)
        self.assertEqual(got, ["B0AAAAAAA2", "B0AAAAAAA1"])

    def test_already_has_third_party_sources_is_excluded(self):
        self._hist([
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 30},
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa2/",
             "impressions": 20},
        ])
        self._asin("B0AAAAAAA1", tp_hosts=2)  # 保有 → 対象外
        self._asin("B0AAAAAAA2", tp_hosts=1)  # 未保有 (hosts<2) → 対象
        got = F._gsc_demand_pool(self.base, history=self.hist, days=28,
                                 min_impressions=10)
        self.assertEqual(got, ["B0AAAAAAA2"])

    def test_band_is_not_a_filter(self):
        # band では絞らない (thin がコーパスの 3/4 で選別になっていない)。
        # 素材ゼロ = zero 相当の ASIN も imp があれば入ること。
        self._hist([
            {"date": "2026-08-16", "page": "https://navi.omcha.jp/products/b0aaaaaaa1/",
             "impressions": 15},
        ])
        self._asin("B0AAAAAAA1")
        self.assertEqual(
            F._gsc_demand_pool(self.base, history=self.hist, days=28, min_impressions=10),
            ["B0AAAAAAA1"],
        )

    def test_missing_history_is_inert(self):
        self.assertEqual(
            F._gsc_demand_pool(self.base, history=self.root / "nope.jsonl"), [])

    def test_pickable_pool_is_untouched_by_this_lane(self):
        # 穴(a): `cand - existing` を外して母集合を融合させていないこと。
        src = pathlib.Path(F.__file__).read_text(encoding="utf-8")
        self.assertIn("return sorted(cand - existing)", src)


if __name__ == "__main__":
    unittest.main()


class MonthUsageTest(unittest.TestCase):
    """月次バジェットの母数 = 書き出し済み fetched_at の当月ぶん。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, asin: str, ts):
        d = self.base / asin
        d.mkdir(parents=True, exist_ok=True)
        payload = {} if ts is None else {"fetched_at": ts}
        (d / F.OUT_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def test_counts_only_current_month(self):
        now = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc)
        self._write("B000000001", "2026-08-01T00:00:00+00:00")
        self._write("B000000002", "2026-08-24T23:59:59+00:00")
        self._write("B000000003", "2026-07-31T23:59:59+00:00")   # 前月
        self._write("B000000004", "2026-09-01T00:00:00+00:00")   # 翌月
        self.assertEqual(F.month_usage(self.base, now=now), 2)

    def test_malformed_and_missing_timestamps_are_ignored(self):
        now = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc)
        self._write("B000000001", "2026-08-01T00:00:00+00:00")
        self._write("B000000002", None)      # fetched_at 無し
        self._write("B000000003", 12345)     # 型違い
        self.assertEqual(F.month_usage(self.base, now=now), 1)

    def test_empty_base_is_zero(self):
        self.assertEqual(F.month_usage(self.base), 0)


class NoticeTest(unittest.TestCase):
    """枠の枯渇を UI に出す (#4793: 緑のまま静かに縮退させない)。"""

    def test_emits_annotation_under_actions(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                mock.patch("sys.stdout", buf):
            F._notice("warning", "枠が尽きました")
        self.assertIn("::warning::枠が尽きました", buf.getvalue())

    def test_silent_on_stdout_when_not_in_actions(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), \
                mock.patch("sys.stdout", buf):
            F._notice("warning", "枠が尽きました")
        self.assertEqual(buf.getvalue(), "")


class MonthlyBudgetCliTest(unittest.TestCase):
    """レーンは 2 本あり別プロセス。budget は共有記録から数えた実消費で効く。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv, usage, fetched):
        """_cli を走らせ、fetch_for_asin の呼び出し回数を返す。"""
        calls = []

        def _fake_fetch(asin, api_key, base, max_sources=8, dry_run=False):
            calls.append(asin)
            return {"asin": asin, "status": "ok", "sources": 3}

        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "k", "GITHUB_ACTIONS": ""}), \
                mock.patch.object(F, "month_usage", return_value=usage), \
                mock.patch.object(F, "fetch_for_asin", _fake_fetch), \
                mock.patch.object(F, "_is_fresh", return_value=False), \
                mock.patch.object(F.time, "sleep", lambda *_: None), \
                mock.patch.object(sys, "argv", ["prog"] + argv):
            rc = F._cli()
        self.assertEqual(rc, 0)
        return calls

    def test_budget_reached_fetches_nothing(self):
        calls = self._run(
            ["B00TARGET1", "--base", str(self.base), "--monthly-budget", "900"],
            usage=900, fetched=0)
        self.assertEqual(calls, [])

    def test_remaining_caps_max_queries(self):
        """残枠 < max-queries なら残枠に合わせる (超過して枠を割らない)。"""
        with mock.patch.object(F, "_pickable_pool",
                               return_value=["B00000000%d" % i for i in range(1, 9)]), \
                mock.patch.object(F._sc, "score_asin", return_value={"band": "zero"}):
            calls = self._run(
                ["--pool", "--base", str(self.base),
                 "--max-queries", "30", "--monthly-budget", "900"],
                usage=897, fetched=0)
        self.assertEqual(len(calls), 3)

    def test_budget_zero_disables_the_guard(self):
        with mock.patch.object(F, "_pickable_pool",
                               return_value=["B00000000%d" % i for i in range(1, 6)]), \
                mock.patch.object(F._sc, "score_asin", return_value={"band": "zero"}):
            calls = self._run(
                ["--pool", "--base", str(self.base),
                 "--max-queries", "4", "--monthly-budget", "0"],
                usage=99999, fetched=0)
        self.assertEqual(len(calls), 4)


class RecordCallTest(unittest.TestCase):
    """実 API 呼び出し回数の共有カウンタ (_tavily_usage.json)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _read(self):
        return json.loads((self.base / F.USAGE_NAME).read_text(encoding="utf-8"))

    def test_increments_within_the_same_month(self):
        now = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(F.record_call(self.base, now=now), 1)
        self.assertEqual(F.record_call(self.base, now=now), 2)
        self.assertEqual(self._read()["month"], "2026-09")

    def test_resets_on_month_rollover(self):
        sep = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
        F.record_call(self.base, now=sep)
        F.record_call(self.base, now=sep)
        nxt = F.record_call(self.base, now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(nxt, 1)

    def test_malformed_counter_restarts_at_one(self):
        (self.base / F.USAGE_NAME).write_text("{ not json", encoding="utf-8")
        now = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(F.record_call(self.base, now=now), 1)

    def test_counter_file_is_not_mistaken_for_an_asin_payload(self):
        """per_asin 直下に置くので glob(*/OUT_NAME) には掛からない。"""
        now = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
        F.record_call(self.base, now=now)
        self.assertEqual(F._fetched_at_usage(self.base, now), 0)


class MonthUsageCounterTest(unittest.TestCase):
    """month_usage は「実呼び出し回数」と「成功件数」の大きい方を返す。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_success(self, asin, ts):
        d = self.base / asin
        d.mkdir(parents=True, exist_ok=True)
        (d / F.OUT_NAME).write_text(json.dumps({"fetched_at": ts}), encoding="utf-8")

    def _write_counter(self, month, calls):
        (self.base / F.USAGE_NAME).write_text(
            json.dumps({"month": month, "calls": calls}), encoding="utf-8")

    def test_counter_wins_when_it_exceeds_successes(self):
        """失敗した呼び出しの分だけ counter が多い。これが本来の消費。"""
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_success("B000000001", "2026-09-01T00:00:00+00:00")
        self._write_counter("2026-09", 40)
        self.assertEqual(F.month_usage(self.base, now=now), 40)

    def test_falls_back_to_successes_when_counter_is_missing(self):
        """導入直後 / カウンタ喪失。過小な 0 で budget を判断させない。"""
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_success("B000000001", "2026-09-01T00:00:00+00:00")
        self._write_success("B000000002", "2026-09-02T00:00:00+00:00")
        self.assertEqual(F.month_usage(self.base, now=now), 2)

    def test_stale_counter_month_is_ignored(self):
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_counter("2026-08", 900)
        self._write_success("B000000001", "2026-09-01T00:00:00+00:00")
        self.assertEqual(F.month_usage(self.base, now=now), 1)


class RawCallCountTest(unittest.TestCase):
    """raw_call_count は成功件数と混ぜず、台帳の calls だけを返す
    (#4841 V2: month_usage を消費差分に使うと fetched_at 側が上回る月に 0 と出た)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_success(self, asin, ts):
        d = self.base / asin
        d.mkdir(parents=True, exist_ok=True)
        (d / F.OUT_NAME).write_text(json.dumps({"fetched_at": ts}), encoding="utf-8")

    def _write_counter(self, month, calls):
        (self.base / F.USAGE_NAME).write_text(
            json.dumps({"month": month, "calls": calls}), encoding="utf-8")

    def test_ignores_fetched_at_success_count_even_when_larger(self):
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_counter("2026-09", 40)
        for i in range(430):
            self._write_success(f"B{i:09d}", "2026-09-01T00:00:00+00:00")
        self.assertEqual(F.month_usage(self.base, now=now), 430)  # 参考: month_usage は大きい方
        self.assertEqual(F.raw_call_count(self.base, now=now), 40)

    def test_missing_counter_is_zero_not_success_count(self):
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_success("B000000001", "2026-09-01T00:00:00+00:00")
        self.assertEqual(F.raw_call_count(self.base, now=now), 0)

    def test_stale_counter_month_is_zero(self):
        now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
        self._write_counter("2026-08", 900)
        self.assertEqual(F.raw_call_count(self.base, now=now), 0)


class EmptyNegativeCacheTest(unittest.TestCase):
    """空振り ASIN の再問い合わせをバックオフさせる (--empty-max-age-days)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        self.p = self.base / F.OUT_NAME

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, days_ago, **extra):
        ts = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=days_ago)).isoformat()
        payload = {"fetched_at": ts}
        payload.update(extra)
        self.p.write_text(json.dumps(payload), encoding="utf-8")

    def test_streak_defaults_to_one_for_legacy_empty_payloads(self):
        """empty_streak を持たない既存ファイルは sources から 1 回ぶん復元する。"""
        self.assertEqual(F._empty_streak({"sources": []}), 1)
        self.assertEqual(F._empty_streak({"sources": [{"host": "a"}]}), 1)
        self.assertEqual(
            F._empty_streak({"sources": [{"host": "a"}, {"host": "b"}]}), 0)
        self.assertEqual(F._empty_streak({"sources": [], "empty_streak": 3}), 3)

    def test_empty_asin_is_skipped_past_the_normal_max_age(self):
        self._write(100, sources=[], empty_streak=1)
        self.assertFalse(F._is_fresh(self.p, 90))
        self.assertTrue(F._is_fresh(self.p, 90, 180))

    def test_backoff_grows_with_the_streak(self):
        self._write(200, sources=[], empty_streak=1)
        self.assertFalse(F._is_fresh(self.p, 90, 180))
        self._write(200, sources=[], empty_streak=2)
        self.assertTrue(F._is_fresh(self.p, 90, 180))

    def test_backoff_is_capped(self):
        cap = F._EMPTY_BACKOFF_CAP
        self._write(180 * cap + 1, sources=[], empty_streak=99)
        self.assertFalse(F._is_fresh(self.p, 90, 180))

    def test_productive_asin_uses_the_normal_max_age(self):
        self._write(100, sources=[{"host": "a"}, {"host": "b"}], empty_streak=0)
        self.assertFalse(F._is_fresh(self.p, 90, 180))

    def test_disabled_when_zero(self):
        self._write(100, sources=[], empty_streak=3)
        self.assertFalse(F._is_fresh(self.p, 90, 0))


class FetchForAsinBookkeepingTest(unittest.TestCase):
    """fetch_for_asin は「叩く前に数え」「空振りの連続回数を持ち越す」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        (self.base / "B00TARGET1").mkdir(parents=True)
        (self.base / "B00TARGET1" / "amazon.json").write_text(
            json.dumps({"item": {"title": "テスト商品"}}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self):
        return json.loads(
            (self.base / "B00TARGET1" / F.OUT_NAME).read_text(encoding="utf-8"))

    def test_call_is_recorded_even_when_the_request_raises(self):
        """credit はレスポンスを待たずに消える。成功後に数えると取りこぼす。"""
        with mock.patch.object(F, "tavily_search", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                F.fetch_for_asin("B00TARGET1", "k", self.base)
        self.assertEqual(F.month_usage(self.base), 1)

    def test_dry_run_records_nothing(self):
        F.fetch_for_asin("B00TARGET1", "k", self.base, dry_run=True)
        self.assertFalse((self.base / F.USAGE_NAME).exists())

    def test_empty_streak_increments_then_resets(self):
        with mock.patch.object(F, "tavily_search", return_value=[]):
            F.fetch_for_asin("B00TARGET1", "k", self.base)
        self.assertEqual(self._payload()["empty_streak"], 1)
        with mock.patch.object(F, "tavily_search", return_value=[]):
            F.fetch_for_asin("B00TARGET1", "k", self.base)
        self.assertEqual(self._payload()["empty_streak"], 2)
        hit = [{"link": "https://a.example/r", "title": "テスト商品の感想", "snippet": "s"},
               {"link": "https://b.example/r", "title": "t", "snippet": "テスト商品を使った"}]
        with mock.patch.object(F, "tavily_search", return_value=hit):
            F.fetch_for_asin("B00TARGET1", "k", self.base)
        self.assertEqual(self._payload()["empty_streak"], 0)


class DailySpendCapTest(unittest.TestCase):
    """日次上限も実呼び出し回数で切る (失敗が続いても枠を超えて投げない)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failing_calls_still_consume_the_daily_limit(self):
        attempts = []

        def _boom(asin, api_key, base, max_sources=8, dry_run=False):
            attempts.append(asin)
            raise RuntimeError("transport error")

        pool = ["B0000000%02d" % i for i in range(1, 21)]
        argv = ["prog", "--pool", "--base", str(self.base),
                "--max-queries", "5", "--monthly-budget", "0"]
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "k", "GITHUB_ACTIONS": ""}), \
                mock.patch.object(F, "month_usage", return_value=0), \
                mock.patch.object(F, "_pickable_pool", return_value=pool), \
                mock.patch.object(F._sc, "score_asin", return_value={"band": "zero"}), \
                mock.patch.object(F, "fetch_for_asin", _boom), \
                mock.patch.object(F, "_is_fresh", return_value=False), \
                mock.patch.object(F.time, "sleep", lambda *_: None), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(F._cli(), 0)
        self.assertEqual(len(attempts), 5)


    def test_waiting_first_party_and_rewrite_are_collected_first(self):
        # #9199: 生成が第三者ソース待ちで止まっている first-party → 書き直し待ち →
        # unfetched → その他 の順。日次上限の外に落ちると待ったまま記事にならない。
        order = []

        def _ok(asin, api_key, base, max_sources=8, dry_run=False):
            order.append(asin)
            return {"asin": asin, "status": "ok", "sources": 2}

        pool = ["B0A0000001", "B0A0000002", "B0A0000003", "B0A0000004", "B0A0000005"]
        bands = {"B0A0000001": "thin", "B0A0000002": "unfetched", "B0A0000003": "thin",
                 "B0A0000004": "zero", "B0A0000005": "thin"}
        prio = {"B0A0000005": 0, "B0A0000004": 1}
        argv = ["prog", "--pool", "--bands", "zero,thin,unfetched", "--base", str(self.base),
                "--max-queries", "10", "--monthly-budget", "0"]
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "k", "GITHUB_ACTIONS": ""}), \
                mock.patch.object(F, "_pickable_pool", return_value=pool), \
                mock.patch.object(F, "_waiting_priority", return_value=prio), \
                mock.patch.object(F._sc, "score_asin", lambda a, base: {"band": bands[a]}), \
                mock.patch.object(F, "fetch_for_asin", _ok), \
                mock.patch.object(F, "_is_fresh", return_value=False), \
                mock.patch.object(F.time, "sleep", lambda *_: None), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(F._cli(), 0)
        self.assertEqual(order, ["B0A0000005", "B0A0000004", "B0A0000002",
                                 "B0A0000001", "B0A0000003"])


class PickablePoolTest(unittest.TestCase):
    """_pickable_pool は 03-invoke-jules と同じ母集合 (first_party_pool を含む)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        root = pathlib.Path(self._tmp.name)
        (root / "data" / "raw").mkdir(parents=True)
        (root / "data" / "articles").mkdir(parents=True)
        os.chdir(root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _write(self, rel: str, obj):
        pathlib.Path(rel).write_text(json.dumps(obj), encoding="utf-8")

    def test_includes_first_party_pool(self):
        self._write("data/raw/amazon.json", {"items": [{"asin": "B0AAAAAAA1"}]})
        self._write("data/raw/ranking_pool.json", {"asins": ["B0BBBBBBB2"]})
        self._write("data/raw/first_party_pool.json",
                    {"asins": ["B0CCCCCCC3", "not-an-asin"]})
        self.assertEqual(F._pickable_pool(),
                         ["B0AAAAAAA1", "B0BBBBBBB2", "B0CCCCCCC3"])

    def test_existing_article_excluded_and_missing_pools_inert(self):
        self._write("data/raw/first_party_pool.json", {"asins": ["B0CCCCCCC3"]})
        pathlib.Path("data/articles/2026-10-06-B0CCCCCCC3.json").write_text("{}")
        self.assertEqual(F._pickable_pool(), [])

    def test_pending_rewrite_included_even_outside_amazon_json(self):
        # 書き直し待ちは既存記事があり amazon.json にも居ないが、03 は pick する
        pathlib.Path("data/rewrite_queue").mkdir(parents=True)
        self._write("data/rewrite_queue/B0DDDDDDD4.json",
                    {"asin": "B0DDDDDDD4", "old_slug": "2026-05-30-B0DDDDDDD4"})
        pathlib.Path("data/articles/2026-05-30-B0DDDDDDD4.json").write_text("{}")
        self.assertEqual(F._pickable_pool(), ["B0DDDDDDD4"])

    def test_waiting_priority_puts_first_party_before_rewrite(self):
        pathlib.Path("data/rewrite_queue").mkdir(parents=True)
        for a in ("B0DDDDDDD4", "B0CCCCCCC3"):
            self._write(f"data/rewrite_queue/{a}.json",
                        {"asin": a, "old_slug": f"2026-05-30-{a}"})
            pathlib.Path(f"data/articles/2026-05-30-{a}.json").write_text("{}")
        self._write("data/raw/first_party_pool.json", {"asins": ["B0CCCCCCC3", "B0EEEEEEE5"]})
        self.assertEqual(F._waiting_priority(),
                         {"B0DDDDDDD4": 1, "B0CCCCCCC3": 0, "B0EEEEEEE5": 0})

    def test_landed_rewrite_excluded(self):
        # 置き換えが着地済み (より新しい本文がある) なら待ちではない
        pathlib.Path("data/rewrite_queue").mkdir(parents=True)
        self._write("data/rewrite_queue/B0DDDDDDD4.json",
                    {"asin": "B0DDDDDDD4", "old_slug": "2026-05-30-B0DDDDDDD4"})
        pathlib.Path("data/articles/2026-05-30-B0DDDDDDD4.json").write_text("{}")
        pathlib.Path("data/articles/2026-10-08-B0DDDDDDD4.json").write_text("{}")
        self.assertEqual(F._pickable_pool(), [])
