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
        with mock.patch.object(F.urllib.request, "urlopen", return_value=resp):
            items = F.tavily_search("レゴ クラシック", "tvly-test", num=10)
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
            srcs = [{"url": f"https://h{i}.example.com/a", "host": f"h{i}.example.com"}
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
        hit = [{"link": "https://a.example/r", "title": "t", "snippet": "s"},
               {"link": "https://b.example/r", "title": "t", "snippet": "s"}]
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
