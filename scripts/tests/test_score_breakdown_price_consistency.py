"""知育スコアの内訳 (正規流通・コスパ) が価格欄 (build_post._resolve_buy_offer) と同じ判定を使う。

固定すること:
  - 正規流通: 価格が出るサイトだけを取扱として数える。市場キャッシュに ASIN があっても
    価格が出なければ数えない。同一商品と未確認 (verified=False) の出品も数えない。
    prices が dict でない記事だけ従来のキャッシュ判定
  - コスパ: 価格欄のボタンと同じ「購入できる最安」(在庫切れ除外) で計算する。在庫切れしか
    無いときは在庫切れ込みの最安で計算し、その旨を文言に出す
  - 共通関数 buy_offer_rules は価格欄の priced / eligible と同じ結果を返す
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_post  # noqa: E402
import buy_offer_rules  # noqa: E402
import score_calculator  # noqa: E402
from brand_normalizer import NormalizedBrand  # noqa: E402

_ASIN = "B0TESTSCR1"
AMZ = {"price": 3000, "url": f"https://www.amazon.co.jp/dp/{_ASIN}/", "availability": "在庫あり。"}
RKT = {"price": 2800, "url": "https://item.rakuten.co.jp/shop/x/", "verified": True, "is_search": False}
YHO = {"price": 2900, "url": "https://store.shopping.yahoo.co.jp/shop/x.html", "verified": True,
       "is_search": False}
NONE = {"price": 0, "url": ""}
OOS_YHO = dict(YHO, price=1000, availability="outofstock")


def _make_brand():
    return NormalizedBrand(canonical="x", tier="B", region="JP")


def _prices(amazon=AMZ, rakuten=NONE, yahoo=NONE) -> dict:
    return {"amazon": amazon, "rakuten": rakuten, "yahoo": yahoo}


def _mm(prices):
    return score_calculator._multi_market_score(
        _ASIN, _make_brand(), pathlib.Path("."), {"prices": prices})


class MultiMarketTest(unittest.TestCase):
    def setUp(self):
        # 市場キャッシュには ASIN が載っている状態 (従来はこれだけで 3 サイト扱いだった)
        self._orig = score_calculator._MARKET_CACHE
        score_calculator._MARKET_CACHE = {"rakuten": {_ASIN}, "yahoo": {_ASIN}}

    def tearDown(self):
        score_calculator._MARKET_CACHE = self._orig

    def test_yahoo_without_price_is_not_counted(self):
        pts, reason = _mm(_prices(AMZ, RKT, NONE))
        self.assertEqual(pts, 7)
        self.assertIn("Amazon と楽天市場", reason)
        self.assertNotIn("3サイト", reason)

    def test_three_priced_sites(self):
        pts, reason = _mm(_prices(AMZ, RKT, YHO))
        self.assertEqual(pts, 10)
        self.assertIn("3サイト", reason)

    def test_unverified_site_is_not_counted(self):
        pts, reason = _mm(_prices(AMZ, dict(RKT, verified=False), YHO))
        self.assertEqual(pts, 7)
        self.assertIn("Yahoo!ショッピング", reason)
        self.assertNotIn("楽天", reason)

    def test_discontinued_site_is_not_counted(self):
        pts, _ = _mm(_prices(AMZ, dict(RKT, discontinued=True), NONE))
        self.assertEqual(pts, 3)

    def test_non_dict_prices_falls_back_to_cache(self):
        pts, _ = score_calculator._multi_market_score(
            _ASIN, _make_brand(), pathlib.Path("."), {"prices": None})
        self.assertEqual(pts, 10)


class MultiMarketAmazonTest(unittest.TestCase):
    def test_amazon_discontinued_with_rakuten_and_yahoo(self):
        amz = {"price": 0, "url": "", "discontinued": True}
        pts, reason = _mm(_prices(amz, RKT, YHO))
        self.assertEqual(pts, 7)
        self.assertIn("楽天市場と Yahoo!ショッピングで取扱", reason)
        self.assertNotIn("3サイト", reason)

    def test_amazon_no_price_with_rakuten_only(self):
        pts, reason = _mm(_prices(NONE, RKT, NONE))
        self.assertEqual(pts, 3)
        self.assertIn("楽天市場のみ", reason)
        self.assertNotIn("Amazon が主要", reason)

    def test_amazon_only(self):
        pts, reason = _mm(_prices(AMZ, NONE, NONE))
        self.assertEqual(pts, 3)
        self.assertIn("Amazon が主要な購入経路", reason)

    def test_no_priced_site(self):
        pts, reason = _mm(_prices(NONE, NONE, NONE))
        self.assertEqual(pts, 3)
        self.assertIn("販売サイトが限られます", reason)
        self.assertNotIn("Amazon が主要", reason)

    def test_calculate_breakdown_matches_price_site_count(self):
        amz = {"price": 0, "url": "", "discontinued": True}
        art = {"product": {"asin": _ASIN, "name": "x", "prices": _prices(amz, RKT, YHO)}}
        res = score_calculator.calculate(art, _make_brand(), asin=_ASIN, repo_root=pathlib.Path("."))
        self.assertEqual(res.breakdown["multi_market"], 7)
        self.assertIn("楽天市場と Yahoo!ショッピング", res.rationale[5])


class PriceValueTest(unittest.TestCase):
    def test_out_of_stock_cheapest_is_ignored(self):
        pts, reason = score_calculator._price_value_score(
            {"prices": _prices(AMZ, NONE, OOS_YHO), "best_price": 1000}, (3, 6))
        self.assertIn("¥3,000", reason)
        self.assertNotIn("¥1,000", reason)
        self.assertNotIn("在庫切れ", reason)

    def test_only_out_of_stock_still_computes_with_note(self):
        amz = dict(AMZ, availability="一時的に在庫切れ; 入荷時期は未定です。")
        pts, reason = score_calculator._price_value_score(
            {"prices": _prices(amz, NONE, NONE)}, (3, 6))
        self.assertIn("現在在庫切れのため", reason)
        self.assertIn("¥3,000", reason)
        self.assertNotEqual(reason, "本記事執筆時点での価格情報を取得中です。最新の価格は本文の比較セクションでご確認ください。")

    def test_no_purchasable_price_is_pending(self):
        pts, reason = score_calculator._price_value_score(
            {"prices": _prices(NONE, NONE, NONE)}, (3, 6))
        self.assertEqual(pts, 5)
        self.assertIn("取得中", reason)

    def test_no_candidate_falls_back_to_best_price_without_note(self):
        # 金額はあるが url 欠落で候補に入らない
        amz = {"price": 4000, "url": ""}
        pts, reason = score_calculator._price_value_score(
            {"prices": _prices(amz, NONE, NONE), "best_price": 4000}, (3, 6))
        self.assertIn("¥4,000", reason)
        self.assertNotIn("在庫切れ", reason)

    def test_non_dict_prices_keeps_legacy(self):
        _pts, reason = score_calculator._price_value_score({"price": 3000}, (3, 6))
        self.assertIn("¥3,000", reason)


class BuyOfferRulesTest(unittest.TestCase):
    def test_matches_resolve_buy_offer(self):
        cases = [_prices(AMZ, RKT, YHO), _prices(AMZ, RKT, OOS_YHO),
                 _prices(AMZ, dict(RKT, verified=False), NONE), _prices(NONE, NONE, NONE)]
        for prices in cases:
            with self.subTest(prices=prices):
                offer = build_post._resolve_buy_offer({"product": {"prices": prices}})
                self.assertEqual(len(buy_offer_rules.priced_site_keys(prices)),
                                 offer["price_site_count"])
                found = buy_offer_rules.cheapest_purchasable(prices)
                primary = offer["primary"]
                if primary and primary["kind"] == "cheapest":
                    self.assertEqual(found, (primary["price"], primary["site"]))
                else:
                    self.assertIsNone(found)

    def test_amazon_stock_state_reexported(self):
        self.assertIs(build_post.amazon_stock_state, buy_offer_rules.amazon_stock_state)


if __name__ == "__main__":
    unittest.main()
