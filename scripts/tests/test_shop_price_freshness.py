"""楽天・Yahoo の価格の鮮度と消えた商品の扱い (#12305)。

K の決定 (2026-11-27・案 1): 確認日が 14 日を超えたショップの価格は、価格・最安値の
表示から外し、ショップへのリンクだけ残す。

守ること:
  - 確かめ直しが書き込みで動き始めてから 8 日たつまでは隠さない (導入直後に大半が消えないように)
  - 14 日を超えたら金額 0・stale=True・リンクは残す・確認日は出さない・最安値に数えない
  - _gone_at があれば照合は不合格。記事に写された古い価格も使わず検索リンクにする
  - 一覧ページ (build_feature_lists) も同じ判定 (market_prices.resolve_price)
  - 価格カードは「最新価格はショップで確認」
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
sys.path.insert(0, str(THIS_DIR))

import build_post  # noqa: E402
import market_prices  # noqa: E402
from test_build_post_buy_offer import _render  # noqa: E402

ASIN = "B0TESTBUY1"
NOW = datetime(2026, 12, 20, 3, 0, tzinfo=timezone.utc)


def _matched(days_ago, **kw):
    m = {"matched_asin": ASIN, "title": "木製 積み木", "price": 2100, "search_keyword": "木製 積み木",
         "url": "https://item.rakuten.co.jp/shop/y/",
         "_refreshed_at": (NOW - timedelta(days=days_ago)).isoformat()}
    m.update(kw)
    return m


class StaleCutoffTest(unittest.TestCase):
    def _cutoff(self, state):
        with tempfile.TemporaryDirectory() as d:
            if state is not None:
                (pathlib.Path(d) / market_prices.SHOP_REFRESH_STATE_FILE).write_text(
                    json.dumps(state), encoding="utf-8")
            return market_prices.stale_cutoff(pathlib.Path(d), NOW)

    def test_off_without_state_or_during_warmup(self):
        self.assertIsNone(self._cutoff(None))
        self.assertIsNone(self._cutoff({"last_run_jst": "2026-12-19"}))
        self.assertIsNone(self._cutoff({"first_write_at": (NOW - timedelta(days=7)).isoformat()}))

    def test_on_after_warmup(self):
        cutoff = self._cutoff({"first_write_at": (NOW - timedelta(days=8)).isoformat()})
        self.assertEqual(cutoff, NOW - timedelta(days=market_prices.SHOP_PRICE_STALE_DAYS))

    def test_is_stale(self):
        cutoff = NOW - timedelta(days=14)
        self.assertFalse(market_prices.is_stale(_matched(13), cutoff))
        self.assertTrue(market_prices.is_stale(_matched(15), cutoff))
        self.assertTrue(market_prices.is_stale(_matched(1, _refreshed_at=None), cutoff))
        self.assertFalse(market_prices.is_stale(_matched(400), None))


class GoneTest(unittest.TestCase):
    def test_gone_fails_quality(self):
        self.assertTrue(market_prices.matched_passes_quality(_matched(1), 2000))
        self.assertFalse(market_prices.matched_passes_quality(_matched(1, _gone_at="2026-12-01"), 2000))

    def test_resolve_price(self):
        cutoff = NOW - timedelta(days=14)
        self.assertEqual(market_prices.resolve_price(1900, _matched(1), 2000, cutoff=cutoff), 2100)
        self.assertEqual(market_prices.resolve_price(1900, _matched(20), 2000, cutoff=cutoff), 0)
        self.assertEqual(market_prices.resolve_price(1900, _matched(20), 2000), 2100)  # cutoff なし
        self.assertEqual(market_prices.resolve_price(1900, _matched(1, _gone_at="x"), 2000, cutoff=cutoff), 0)


class AttachTest(unittest.TestCase):
    def _data(self, yahoo=None):
        prices = {"amazon": {"price": 2000, "url": f"https://www.amazon.co.jp/dp/{ASIN}/"}}
        if yahoo:
            prices["yahoo"] = yahoo
        return {"product": {"asin": ASIN, "name": "木製 積み木", "prices": prices}}

    def test_fresh_price_is_shown(self):
        data = self._data()
        build_post._attach_market_prices(data, {ASIN: _matched(3)}, {}, NOW - timedelta(days=14))
        r = data["product"]["prices"]["rakuten"]
        self.assertEqual((r["price"], r["stale"]), (2100, False))
        self.assertIsNotNone(r["checked_at"])

    def test_stale_price_keeps_link_only(self):
        data = self._data()
        build_post._attach_market_prices(data, {ASIN: _matched(20, price=1500)}, {}, NOW - timedelta(days=14))
        r = data["product"]["prices"]["rakuten"]
        self.assertEqual((r["price"], r["stale"], r["verified"], r["is_search"]), (0, True, True, False))
        self.assertEqual(r["url"], "https://item.rakuten.co.jp/shop/y/")
        self.assertIsNone(r["checked_at"])
        # 同じ時点の在庫・送料・あす楽のバッジも出さない
        self.assertEqual((r["availability"], r["free_shipping"], r["asuraku"]), (None, None, None))
        self.assertEqual(data["product"]["best_platform"], "Amazon")  # 古い安値を最安にしない
        self.assertIsNone(build_post._price_checked_on("rakuten", r))

    def test_hidden_cheapest_does_not_leave_old_best_price(self):
        # Amazon に価格が無く、最安だった楽天が古くて隠れた → 古い最安値を残さない (JSON-LD に出るため)
        data = {"product": {"asin": ASIN, "name": "木製 積み木", "best_price": 1500,
                            "best_platform": "楽天市場",
                            "prices": {"amazon": {"price": 0, "url": ""}}}}
        build_post._attach_market_prices(data, {ASIN: _matched(20, price=1500)}, {}, NOW - timedelta(days=14))
        self.assertEqual((data["product"]["best_price"], data["product"]["best_platform"]), (0, ""))

    def test_no_cutoff_keeps_old_behaviour(self):
        data = self._data()
        build_post._attach_market_prices(data, {ASIN: _matched(200)}, {})
        self.assertEqual(data["product"]["prices"]["rakuten"]["price"], 2100)

    def test_gone_uses_search_link_not_article_price(self):
        article_yahoo = {"price": 1800, "url": "https://store.shopping.yahoo.co.jp/s/z.html"}
        data = self._data(yahoo=article_yahoo)
        gone = _matched(1, _gone_at="2026-12-10T00:00:00+00:00",
                        url="https://store.shopping.yahoo.co.jp/s/z.html")
        build_post._attach_market_prices(data, {}, {ASIN: gone})
        y = data["product"]["prices"]["yahoo"]
        self.assertTrue(y["is_search"])
        self.assertEqual(y["price"], 0)
        self.assertNotEqual(y["url"], article_yahoo["url"])

    def test_card_says_check_at_shop(self):
        data = self._data()
        build_post._attach_market_prices(data, {ASIN: _matched(20)}, {}, NOW - timedelta(days=14))
        self.assertIn("最新価格はショップで確認", _render(data))


if __name__ == "__main__":
    unittest.main()
