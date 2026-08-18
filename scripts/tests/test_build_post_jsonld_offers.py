"""#4826 項目6: 有効価格ゼロのとき JSON-LD の offers を残さない。

表示は「取り扱い確認」なのに構造化データだけが古い価格を主張する経路を塞ぐ。
#5130 で潰した「買えないものを買えるかのように出さない」と同じクラスだが、
構造化データ側は読者に見えないぶん気付けない。

2026-08-18 時点でこの経路は発火しない — build 時点で有効価格ゼロになる記事は
67 件あるが、2,085 記事のいずれも `jsonld.product.offers` を持っていない
(offers を作るのは `_fill_jsonld` 自身だけ)。生成側が offers を書き始めた瞬間に
初めて成立する穴なので、ここは「偶然そうなっている」に頼らないためのガード。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", ".."))
sys.path.insert(0, os.path.join(THIS_DIR, ".."))

import build_post as bp  # noqa: E402


def _article(*, prices=None, best_price=None, offers=None):
    product = {"name": "テスト商品", "image": "x.jpg"}
    if prices is not None:
        product["prices"] = prices
    if best_price is not None:
        product["best_price"] = best_price
    data = {"title": "t", "meta_description": "d", "product": product}
    if offers is not None:
        data["jsonld"] = {"product": {"@type": "Product", "offers": offers}}
    return data


def _offers(data):
    return ((data.get("jsonld") or {}).get("product") or {}).get("offers")


def test_offers_dropped_when_no_valid_price():
    """記事 JSON が古い価格の offers を持っていても、価格ゼロなら消す。"""
    data = _article(
        prices={"amazon": {"price": 0}},
        offers={"@type": "AggregateOffer", "lowPrice": "2680", "highPrice": "2680",
                "priceCurrency": "JPY", "offerCount": "1"},
    )
    bp._fill_jsonld(data)
    assert _offers(data) is None


def test_offers_dropped_when_price_is_search_only():
    """is_search (検索リンクであって価格ではない) は有効価格に数えない。"""
    data = _article(
        prices={"rakuten": {"price": 3980, "is_search": True}},
        offers={"@type": "AggregateOffer", "lowPrice": "3980"},
    )
    bp._fill_jsonld(data)
    assert _offers(data) is None


def test_offers_written_when_prices_exist():
    data = _article(prices={"amazon": {"price": 1200}, "rakuten": {"price": 1500}})
    bp._fill_jsonld(data)
    offers = _offers(data)
    assert offers["lowPrice"] == "1200"
    assert offers["highPrice"] == "1500"
    assert offers["offerCount"] == "2"
    assert offers["priceCurrency"] == "JPY"


def test_offers_falls_back_to_best_price():
    """prices が無くても best_price があれば 1 件として出す (従来挙動)。"""
    data = _article(best_price=990)
    bp._fill_jsonld(data)
    offers = _offers(data)
    assert offers["lowPrice"] == offers["highPrice"] == "990"
    assert offers["offerCount"] == "1"


def test_existing_offers_overwritten_not_merged_with_stale_price():
    """価格があるときは古い値が残らない (更新であって併存ではない)。"""
    data = _article(
        prices={"amazon": {"price": 1200}},
        offers={"@type": "AggregateOffer", "lowPrice": "9999", "highPrice": "9999",
                "offerCount": "3"},
    )
    bp._fill_jsonld(data)
    offers = _offers(data)
    assert offers["lowPrice"] == "1200"
    assert offers["offerCount"] == "1"


def test_no_offers_key_created_when_price_is_zero_and_none_existed():
    """元から offers が無ければ、消す処理を入れても増えない。"""
    data = _article(prices={"amazon": {"price": 0}})
    bp._fill_jsonld(data)
    assert "offers" not in (data["jsonld"]["product"])
