"""navi-brain#165 T3/T4/T5: 購入ボタンの箱 (.buy-box) の行き先と、💴 価格の節の見出し・導入文。

固定すること:
  - 主ボタンは「価格があり、販売終了・在庫切れ・検索リンク・同一商品と未確認
    (verified=False) のどれでもない」サイトの最安 1 つ。無ければ Amazon の商品ページ、
    それも無ければ検索リンク (控えめな見た目)
  - purchase_unavailable の記事ではアフィリエイトのボタンを出さず「類似のおすすめ ↓」だけ
  - 価格の出るサイトが 2 以上なら H2 は「価格を3サイトで比較」のまま (導入文だけ合わせる)。
    1 以下なら H2 を「価格と販売サイト」にする (サイト数 0〜3 をテンプレートで描画して確かめる)
  - 購入ボタンは冒頭 / まとめの直後 / FAQ の後に出て、中盤のボタンには金額を出さない
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

import jinja2

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_post  # noqa: E402

_ASIN = "B0TESTBUY1"


def _prices(amazon=None, rakuten=None, yahoo=None) -> dict:
    return {
        "amazon": amazon if amazon is not None else {"price": 0, "url": ""},
        "rakuten": rakuten if rakuten is not None else {"price": 0, "url": ""},
        "yahoo": yahoo if yahoo is not None else {"price": 0, "url": ""},
    }


def _data(prices: dict, **extra) -> dict:
    d = {"product": {"asin": _ASIN, "name": "テスト商品", "prices": prices}}
    d.update(extra)
    return d


AMZ = {"price": 3000, "url": f"https://www.amazon.co.jp/dp/{_ASIN}/", "availability": "在庫あり。"}
RKT = {"price": 2800, "url": "https://item.rakuten.co.jp/shop/x/", "verified": True, "is_search": False}
YHO = {"price": 2900, "url": "https://store.shopping.yahoo.co.jp/shop/x.html", "verified": True,
       "is_search": False}
RKT_SEARCH = {"price": 0, "url": "https://search.rakuten.co.jp/search/mall/x/", "is_search": True}
YHO_SEARCH = {"price": 0, "url": "https://shopping.yahoo.co.jp/search?p=x", "is_search": True}


class ResolveBuyOfferTest(unittest.TestCase):
    def test_cheapest_verified_site_is_primary(self):
        offer = build_post._resolve_buy_offer(_data(_prices(AMZ, RKT, YHO)))
        self.assertEqual(offer["primary"]["site"], "rakuten")
        self.assertEqual(offer["primary"]["kind"], "cheapest")
        self.assertEqual(offer["primary"]["price"], 2800)
        self.assertEqual([o["site"] for o in offer["others"]], ["amazon", "yahoo"])
        self.assertFalse(offer["unavailable"])

    def test_unverified_cheaper_price_is_not_primary(self):
        rkt = dict(RKT, price=1000, verified=False)
        offer = build_post._resolve_buy_offer(_data(_prices(AMZ, rkt, YHO)))
        self.assertEqual(offer["primary"]["site"], "yahoo")
        # 未確認のサイトは文字リンクにも出さない
        self.assertNotIn("rakuten", [o["site"] for o in offer["others"]])

    def test_out_of_stock_and_search_are_skipped(self):
        amz = dict(AMZ, price=1000, availability="一時的に在庫切れ; 入荷時期は未定です。")
        offer = build_post._resolve_buy_offer(_data(_prices(amz, RKT_SEARCH, YHO)))
        self.assertEqual(offer["primary"]["site"], "yahoo")
        self.assertEqual([o["site"] for o in offer["others"]], [])

    def test_tie_prefers_amazon(self):
        offer = build_post._resolve_buy_offer(_data(_prices(AMZ, dict(RKT, price=3000), None)))
        self.assertEqual(offer["primary"]["site"], "amazon")

    def test_falls_back_to_amazon_direct_link(self):
        amz = dict(AMZ, availability="一時的に在庫切れ; 入荷時期は未定です。")
        offer = build_post._resolve_buy_offer(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH)))
        self.assertEqual(offer["primary"]["kind"], "direct")
        self.assertEqual(offer["primary"]["site"], "amazon")
        self.assertEqual(offer["primary"]["price"], 0)
        self.assertEqual(offer["primary"]["text"], "Amazonで再入荷を確認")

    def test_falls_back_to_search_link(self):
        amz = {"price": 0, "url": "", "discontinued": True,
               "search_url": "https://www.amazon.co.jp/s?k=x&tag=t-22"}
        offer = build_post._resolve_buy_offer(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH)))
        self.assertEqual(offer["primary"]["kind"], "search")
        self.assertEqual(offer["primary"]["url"], "https://www.amazon.co.jp/s?k=x&tag=t-22")

    def test_purchase_unavailable(self):
        amz = {"price": 0, "url": "", "discontinued": True}
        offer = build_post._resolve_buy_offer(
            _data(_prices(amz, RKT_SEARCH, YHO_SEARCH), purchase_unavailable=True))
        self.assertTrue(offer["unavailable"])

    def test_price_site_count(self):
        cases = [
            (_prices(AMZ, RKT, YHO), 3),
            (_prices(AMZ, RKT, YHO_SEARCH), 2),
            (_prices(AMZ, RKT_SEARCH, YHO_SEARCH), 1),
            (_prices({"price": 0, "url": "", "discontinued": True}, RKT_SEARCH, YHO_SEARCH), 0),
            # 未確認でも価格カードに金額が出るので数える
            (_prices(AMZ, dict(RKT, verified=False), YHO_SEARCH), 2),
        ]
        for prices, expected in cases:
            with self.subTest(expected=expected):
                offer = build_post._resolve_buy_offer(_data(prices))
                self.assertEqual(offer["price_site_count"], expected)


def _render(data: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(SCRIPTS_DIR / "templates")),
        autoescape=False, keep_trailing_newline=True)
    env.filters["amazon_stock_state"] = build_post.amazon_stock_state
    ctx = {
        "slug": f"2026-01-01-{_ASIN}", "amazon_partner_tag": "chk01-22",
        "narrative": {"closing": "買ってよい一品です。"},
        "faq": [{"question": "Q", "answer": "A"}],
        "score": {"total_100": 80, "education": 4.0, "longevity": 4.0, "safety": 4.0,
                  "cost_performance": 4.0, "radar_points": "200,50 290,140 200,230 110,140"},
        "verdict": {"headline": "迷ったらこれ"},
        "price_checked_at": "2026-11-13",
    }
    ctx.update(data)
    product = ctx["product"]
    product.setdefault("best_price", 0)
    product.setdefault("best_platform", "")
    ctx["buy_offer"] = build_post._resolve_buy_offer(ctx)
    return env.get_template("post.md.j2").render(**ctx)


def _price_h2(md: str) -> str:
    m = re.search(r"^## <small>テスト商品の</small>💴 (.+)$", md, re.M)
    assert m, "💴 の H2 が無い"
    return m.group(1)


def _price_intro(md: str) -> str:
    m = re.search(r"^## <small>テスト商品の</small>💴 .+\n\n(.+)$", md, re.M)
    assert m
    return m.group(1)


class PriceHeadingRenderTest(unittest.TestCase):
    """T5: サイト数 0〜3 のそれぞれで H2 と導入文を描画して確かめる。"""

    def test_three_sites(self):
        md = _render(_data(_prices(AMZ, RKT, YHO)))
        self.assertEqual(_price_h2(md), "価格を3サイトで比較")
        self.assertEqual(_price_intro(md),
                         "当サイトが自動観測した最新価格をAmazon・楽天・Yahoo!ショッピングで横断比較しました。")

    def test_two_sites(self):
        md = _render(_data(_prices(AMZ, RKT, YHO_SEARCH)))
        self.assertEqual(_price_h2(md), "価格を3サイトで比較")
        self.assertEqual(_price_intro(md),
                         "当サイトが自動観測した最新価格をAmazon・楽天で比較しました。"
                         "Yahoo!ショッピングでは同じ商品の価格を確認できませんでした。")

    def test_one_site(self):
        md = _render(_data(_prices(AMZ, RKT_SEARCH, YHO_SEARCH)))
        self.assertEqual(_price_h2(md), "価格と販売サイト")
        self.assertNotIn("3サイトで比較", md)
        self.assertEqual(_price_intro(md),
                         "当サイトの自動観測で価格を確認できたのはAmazonだけです。"
                         "楽天・Yahoo!ショッピングでは同じ商品の価格を確認できませんでした。")

    def test_zero_sites(self):
        amz = {"price": 0, "url": "", "discontinued": True,
               "search_url": "https://www.amazon.co.jp/s?k=x&tag=chk01-22"}
        md = _render(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH)))
        self.assertEqual(_price_h2(md), "価格と販売サイト")
        self.assertEqual(_price_intro(md),
                         "当サイトの自動観測では、Amazonは取り扱い終了、楽天・Yahoo!ショッピングでは"
                         "同じ商品の価格を確認できませんでした。各サイトでの取り扱いは下のリンクからご確認ください。")

    def test_unverified_price_is_noted_in_intro(self):
        md = _render(_data(_prices(AMZ, dict(RKT, verified=False), YHO)))
        self.assertEqual(_price_h2(md), "価格を3サイトで比較")
        self.assertEqual(_price_intro(md),
                         "当サイトが自動観測した最新価格をAmazon・楽天・Yahoo!ショッピングで横断比較しました。"
                         "楽天の価格は同じ商品か確認できていません。")

    def test_zero_sites_without_discontinued(self):
        md = _render(_data(_prices({"price": 0, "url": f"https://www.amazon.co.jp/dp/{_ASIN}/"},
                                   RKT_SEARCH, YHO_SEARCH)))
        self.assertEqual(_price_h2(md), "価格と販売サイト")
        self.assertIn("いずれでも同じ商品の価格を確認できませんでした", _price_intro(md))


class BuyBoxRenderTest(unittest.TestCase):
    def test_slots_and_price_only_at_top(self):
        md = _render(_data(_prices(AMZ, RKT, YHO)))
        boxes = re.findall(r'<div class="buy-box buy-box--(\w+)">(.*?)</div>', md)
        variants = [v for v, _ in boxes]
        self.assertEqual(variants[0], "top")
        self.assertIn("closing", variants)
        self.assertIn("faq", variants)
        for variant, body in boxes:
            with self.subTest(variant=variant):
                self.assertIn('data-cta-slot="buy-', body)
                self.assertIn('href="#price-compare"', body)
                self.assertNotIn("￥", body)  # ボタンの箱に金額は出さない
        # 金額は冒頭の決める箱の一行にだけ出る
        self.assertIn('<p class="decide-box-price">🏷️ 最安値 <strong>￥2,800</strong>（楽天市場・2026-11-13 時点）</p>',
                      md)
        self.assertNotIn("best-deal", md)
        self.assertIn('id="price-compare"', md)
        # [PR] の断り書きは最初のアフィリエイトリンクより前
        self.assertLess(md.index("[PR]"), md.index('rel="noopener nofollow sponsored"'))
        # まとめの直後に buy-closing
        closing = md.index("💡 まとめ：買うべき？")
        self.assertLess(closing, md.index('data-cta-slot="buy-closing"'))
        self.assertLess(md.index('data-cta-slot="buy-closing"'), md.index("❓ よくある質問"))

    def test_mid_box_only_where_the_reader_decides(self):
        narrative = {"closing": "c", "how_to_choose": "h", "why_this_product": "w",
                     "daily_use": "d", "gift_appeal": "g"}
        extra = dict(review_signals={"summary_one_line": "x", "high_points": [{"text": "良い"}]},
                     omcha_related=[{"url": "https://omcha.jp/x", "title": "t"}])

        def slots(md):
            return re.findall(r'data-cta-slot="(buy-[\w-]+)" data-buy-site', md)

        # 🆚 があれば、中盤はその後の 1 つだけ (🧭 の後には出さない)
        md = _render(_data(_prices(AMZ, RKT, YHO), competitive_analysis=[{"name": "類似品"}],
                           narrative=narrative, **extra))
        self.assertEqual(slots(md), ["buy-top", "buy-mid-compare", "buy-closing", "buy-faq"])
        self.assertLess(md.index('id="alternatives"'), md.index('data-cta-slot="buy-mid-compare"'))
        self.assertLess(md.index('data-cta-slot="buy-mid-compare"'), md.index("🧭 選び方"))
        # 🆚 が無ければ 🧭 選び方の後に 1 つ
        md = _render(_data(_prices(AMZ, RKT, YHO), narrative=narrative, **extra))
        self.assertEqual(slots(md), ["buy-top", "buy-mid-choose", "buy-closing", "buy-faq"])
        self.assertLess(md.index("🧭 選び方"), md.index('data-cta-slot="buy-mid-choose"'))
        # 🆚 だけで 🧭 が無い記事も 🆚 の後の 1 つ。中盤のボタンに金額は出さない
        md = _render(_data(_prices(AMZ, RKT, YHO), competitive_analysis=[{"name": "類似品"}],
                           narrative={"closing": "c"}))
        self.assertEqual(slots(md), ["buy-top", "buy-mid-compare", "buy-closing", "buy-faq"])
        mid = re.search(r'<div class="buy-box buy-box--mid">.*?</div>', md).group(0)
        self.assertNotIn("￥", mid)
        self.assertIn('href="#price-compare"', mid)
        # 販売終了の記事は中盤に何も出さない
        amz = {"price": 0, "url": "", "discontinued": True}
        md = _render(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH), purchase_unavailable=True,
                           competitive_analysis=[{"name": "類似品"}], narrative=narrative))
        self.assertNotIn("buy-box--mid", md)
        # どちらも無ければ中盤は無し
        md = _render(_data(_prices(AMZ, RKT, YHO), narrative={"closing": "c", "gift_appeal": "g"}))
        self.assertEqual(slots(md), ["buy-top", "buy-closing", "buy-faq"])

    def test_unavailable_shows_only_alternatives_link(self):
        amz = {"price": 0, "url": "", "discontinued": True,
               "search_url": "https://www.amazon.co.jp/s?k=x&tag=chk01-22"}
        md = _render(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH), purchase_unavailable=True,
                           competitive_analysis=[{"name": "類似品"}]))
        self.assertNotIn("buy-box-primary", md)
        self.assertIn('<a class="buy-box-alt" href="#alternatives">類似のおすすめを見る ↓</a>', md)
        # 買えない商品では同じリンクを冒頭とまとめの後の 2 か所だけにする
        self.assertEqual(md.count("buy-box-alt"), 2)

    def test_unavailable_without_alternatives_has_no_box(self):
        amz = {"price": 0, "url": "", "discontinued": True}
        md = _render(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH), purchase_unavailable=True))
        self.assertNotIn("buy-box", md)

    def test_search_fallback_is_muted(self):
        amz = {"price": 0, "url": "", "discontinued": True,
               "search_url": "https://www.amazon.co.jp/s?k=x&tag=chk01-22"}
        md = _render(_data(_prices(amz, RKT_SEARCH, YHO_SEARCH)))
        self.assertIn("buy-box-primary--muted", md)
        self.assertIn("Amazonで検索 →", md)


if __name__ == "__main__":
    unittest.main()
