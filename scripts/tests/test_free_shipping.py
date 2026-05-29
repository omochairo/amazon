"""Tests for extract_free_shipping (Issue #784).

PR #761 で導入した `offersV2.listings.deliveryInfo.isFreeShippingEligible`
は Creator API で invalid resource だったため hotfix PR #783 で削除。代替案
として `offersV2.listings.merchantInfo.name` が Amazon 直販系のとき
free_shipping=True を返すよう改修した (#784)。
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_amazon import (  # noqa: E402
    AMAZON_DIRECT_SELLER_PATTERNS,
    extract_free_shipping,
)


def _item(seller_name: str | None) -> dict:
    listings: list[dict] = [{}]
    if seller_name is not None:
        listings[0]["merchantInfo"] = {"name": seller_name}
    return {"offersV2": {"listings": listings}}


def test_free_shipping_true_for_amazon_direct():
    assert extract_free_shipping(_item("Amazon.co.jp")) is True


def test_free_shipping_true_for_amazon_japan_english_variant():
    assert extract_free_shipping(_item("Amazon Japan G.K.")) is True


def test_free_shipping_true_for_katakana_amazon():
    assert extract_free_shipping(_item("アマゾンジャパン合同会社")) is True


def test_free_shipping_true_for_amazon_outlet():
    assert extract_free_shipping(_item("amazonアウトレット")) is True


def test_free_shipping_false_for_manufacturer_direct():
    # 任天堂は TRUSTED_SELLER_PATTERNS だが送料無料は保証されない
    assert extract_free_shipping(_item("任天堂")) is False
    assert extract_free_shipping(_item("タカラトミー公式")) is False


def test_free_shipping_false_for_third_party_seller():
    assert extract_free_shipping(_item("不明な転売ヤー")) is False


def test_free_shipping_false_when_merchant_missing():
    # offersV2.listings[].merchantInfo が無い → seller="" → False
    assert extract_free_shipping(_item(None)) is False


def test_free_shipping_false_when_listings_empty():
    assert extract_free_shipping({"offersV2": {"listings": []}}) is False


def test_free_shipping_false_when_offersV2_missing():
    assert extract_free_shipping({}) is False


def test_amazon_direct_patterns_are_lowercase():
    # 定数 invariant: extract_free_shipping は seller.lower() を比較に使うので
    # AMAZON_DIRECT_SELLER_PATTERNS は小文字 / カタカナで保持されているはず。
    for p in AMAZON_DIRECT_SELLER_PATTERNS:
        if any("a" <= c <= "z" or "A" <= c <= "Z" for c in p):
            assert p == p.lower(), f"pattern {p!r} must be lowercase"


def test_case_insensitive_matching():
    # 大文字小文字混在の merchant 名でも判定が安定すること
    assert extract_free_shipping(_item("AMAZON.CO.JP")) is True
    assert extract_free_shipping(_item("amazon.co.jp")) is True
    assert extract_free_shipping(_item("Amazon.Co.Jp")) is True
