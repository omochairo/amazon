"""ショップごとの「価格を取った日」(price_*_checked_at) の付け方と表示。

固定すること:
  - 楽天・Yahoo の照合済み価格には、照合データの _refreshed_at の日付を付ける。
    記事の書き手が写した未確認の価格 (verified=False) には日付を付けない
  - Amazon の日付は _apply_price_overlay が採用した観測の時刻 (price_observed_at)
  - 冒頭の「最安値 ￥… (ショップ・日付 時点)」は、最安のショップ自身の日付を出す
    (以前は楽天が最安でも Amazon の日付を付けていた)
  - 価格カードの下の「💹 価格情報」は、金額が出ていて日付が分かるショップだけをショップ名付きで並べる
  - front matter (= index.json) にはショップごとの price_<shop>_checked_at を載せる
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
sys.path.insert(0, str(THIS_DIR))

import build_post  # noqa: E402
from test_build_post_buy_offer import _data, _prices, _render  # noqa: E402

ASIN = "B0TESTBUY1"
# 観測は UTC 11/25 21:10 = 日本時間 11/26 06:10。表示は日本時間の日付 (#12283)
AMZ = {"price": 3000, "url": f"https://www.amazon.co.jp/dp/{ASIN}/", "availability": "在庫あり。",
       "price_observed_at": "2026-11-25T21:10:00+00:00"}
RKT = {"price": 2800, "url": "https://item.rakuten.co.jp/shop/x/", "verified": True, "is_search": False,
       "checked_at": "2026-08-14"}
YHO = {"price": 2900, "url": "https://store.shopping.yahoo.co.jp/shop/x.html", "verified": True,
       "is_search": False, "checked_at": "2026-11-02"}


class DatePartTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(build_post._date_part("2026-05-30T13:47:11.379837+00:00"), "2026-05-30")
        self.assertEqual(build_post._date_part("2026-05-30"), "2026-05-30")
        # 日本時間の日付にする (#12283)。UTC の 15 時以降は日本時間では翌日
        self.assertEqual(build_post._date_part("2026-05-30T15:00:00+00:00"), "2026-05-31")
        self.assertEqual(build_post._date_part("2026-05-30T14:59:59Z"), "2026-05-30")
        self.assertEqual(build_post._date_part("2026-05-30T15:00:00"), "2026-05-31")   # 時差なしは UTC
        self.assertEqual(build_post._date_part("2026-05-30T09:00:00+09:00"), "2026-05-30")
        for bad in (None, "", "05/30", 20260530, "2026-5-3", "2026-05-30Tgarbage", "2026-02-30"):
            with self.subTest(bad=bad):
                self.assertIsNone(build_post._date_part(bad))


class PriceCheckedOnTest(unittest.TestCase):
    def test_amazon_uses_observation_time(self):
        self.assertEqual(build_post._price_checked_on("amazon", AMZ), "2026-11-26")
        self.assertIsNone(build_post._price_checked_on("amazon", {"price": 3000}))

    def test_market_uses_checked_at(self):
        self.assertEqual(build_post._price_checked_on("rakuten", RKT), "2026-08-14")
        # 記事の書き手が写した価格には日付が無い
        self.assertIsNone(build_post._price_checked_on("yahoo", {"price": 2900, "verified": False}))
        self.assertIsNone(build_post._price_checked_on("yahoo", None))
        # 未確認の価格に日付が紛れ込んでいても出さない
        self.assertIsNone(build_post._price_checked_on(
            "rakuten", {"price": 2800, "verified": False, "checked_at": "2026-11-01"}))


class AttachMarketPricesTest(unittest.TestCase):
    def _data(self):
        return {"product": {"asin": ASIN, "name": "木製 積み木", "prices": {
            "amazon": {"price": 2000, "url": f"https://www.amazon.co.jp/dp/{ASIN}/"},
        }}}

    def test_matched_price_carries_refreshed_date(self):
        data = self._data()
        matched = {"matched_asin": ASIN, "title": "木製 積み木", "price": 2100,
                   "search_keyword": "木製 積み木", "url": "https://item.rakuten.co.jp/shop/y/",
                   "_refreshed_at": "2026-05-30T13:47:11.379837+00:00"}
        build_post._attach_market_prices(data, {ASIN: matched}, {})
        r = data["product"]["prices"]["rakuten"]
        self.assertTrue(r["verified"])
        self.assertEqual(r["checked_at"], "2026-05-30")

    def test_matched_without_refreshed_date_has_none(self):
        data = self._data()
        matched = {"matched_asin": ASIN, "title": "木製 積み木", "price": 2100,
                   "search_keyword": "木製 積み木", "url": "https://item.rakuten.co.jp/shop/y/"}
        build_post._attach_market_prices(data, {ASIN: matched}, {})
        self.assertIsNone(data["product"]["prices"]["rakuten"]["checked_at"])

    def test_unverified_article_price_gets_no_date(self):
        data = self._data()
        data["product"]["prices"]["yahoo"] = {"price": 2050, "url": "https://store.shopping.yahoo.co.jp/s/z.html"}
        build_post._attach_market_prices(data, {}, {})
        y = data["product"]["prices"]["yahoo"]
        self.assertFalse(y["verified"])
        self.assertIsNone(build_post._price_checked_on("yahoo", y))


class PriceCheckedNoteTest(unittest.TestCase):
    def test_lists_each_priced_shop_with_its_own_date(self):
        note = build_post._price_checked_note(_data(_prices(AMZ, RKT, YHO)))
        self.assertEqual(note, "Amazon 2026-11-26 時点・楽天市場 2026-08-14 時点・Yahoo!ショッピング 2026-11-02 時点")

    def test_skips_shops_without_price_or_date(self):
        unverified = {"price": 2700, "url": "https://store.shopping.yahoo.co.jp/s/z.html", "verified": False}
        note = build_post._price_checked_note(_data(_prices(AMZ, {"price": 0, "url": ""}, unverified)))
        self.assertEqual(note, "Amazon 2026-11-26 時点")

    def test_discontinued_amazon_is_skipped(self):
        note = build_post._price_checked_note(_data(_prices(dict(AMZ, discontinued=True), RKT, None)))
        self.assertEqual(note, "楽天市場 2026-08-14 時点")

    def test_none_when_no_date_known(self):
        self.assertIsNone(build_post._price_checked_note(_data(_prices({"price": 3000, "url": "x"}))))


class DiscontinuedAmazonTest(unittest.TestCase):
    def test_no_note_when_only_discontinued_amazon_had_a_date(self):
        # Amazon が取り扱い終了 (価格 0) になっても price_checked_at は残る。
        # その日付を「💹 価格情報」に出さない (Codex・agy の指摘)
        amz = dict(AMZ, price=0, discontinued=True)
        data = _data(_prices(amz, {"price": 2700, "url": "https://item.rakuten.co.jp/s/z/",
                                   "verified": False}, None))
        data["price_checked_note"] = build_post._price_checked_note(data)
        self.assertIsNone(data["price_checked_note"])
        md = _render(data)  # _render は price_checked_at="2026-11-13" を渡す
        self.assertNotIn("💹 価格情報", md)


class RenderTest(unittest.TestCase):
    def test_cheapest_line_uses_cheapest_shops_date(self):
        data = _data(_prices(AMZ, RKT, YHO))
        data["price_checked_note"] = build_post._price_checked_note(data)
        md = _render(data)
        self.assertIn("🏷️ 最安値 <strong>￥2,800</strong>（楽天市場・2026-08-14 時点）", md)
        self.assertIn("💹 価格情報: Amazon 2026-11-26 時点・楽天市場 2026-08-14 時点・"
                      "Yahoo!ショッピング 2026-11-02 時点", md)

    def test_cheapest_line_without_date_has_no_stamp(self):
        md = _render(_data(_prices(AMZ, dict(RKT, checked_at=None), YHO)))
        self.assertIn("🏷️ 最安値 <strong>￥2,800</strong>（楽天市場）", md)

    def test_amazon_cheapest_uses_amazon_date(self):
        md = _render(_data(_prices(dict(AMZ, price=2000), RKT, YHO)))
        self.assertIn("🏷️ 最安値 <strong>￥2,000</strong>（Amazon・2026-11-26 時点）", md)


class FrontMatterTest(unittest.TestCase):
    def test_per_shop_checked_at(self):
        data = {"product": {"asin": ASIN, "name": "テスト商品",
                            "prices": _prices(AMZ, RKT, dict(YHO, checked_at=None))}}
        meta = build_post._frontmatter_meta(data, f"2026-01-01-{ASIN}", False, {}, pathlib.Path("x.json"))
        self.assertEqual(meta["price_amazon_checked_at"], "2026-11-26")
        self.assertEqual(meta["price_rakuten_checked_at"], "2026-08-14")
        self.assertNotIn("price_yahoo_checked_at", meta)
        self.assertEqual(meta["price_yahoo"], 2900)


if __name__ == "__main__":
    unittest.main()
