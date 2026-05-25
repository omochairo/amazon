"""Tests for reseller-ASIN detection in fetch_amazon + quality_gate.

Anchors on the real-world B0G398BYV6 case (Switch Mario Wonder, third-party
listing at ¥8480 while the official ASIN B0C8Y9THVS is ¥5980 on Rakuten).
"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_amazon import (  # noqa: E402
    extract_seller,
    is_trusted_seller,
    is_low_stock_reseller_signal,
    _load_asin_blocklist,
)
from quality_gate import check_no_reseller_pricing  # noqa: E402


def test_extract_seller_from_paapi_shape():
    item = {
        "offersV2": {
            "listings": [
                {"merchantInfo": {"name": "Amazon.co.jp"}}
            ]
        }
    }
    assert extract_seller(item) == "Amazon.co.jp"


def test_extract_seller_returns_empty_when_missing():
    assert extract_seller({}) == ""
    assert extract_seller({"offersV2": {"listings": []}}) == ""


def test_is_trusted_seller_recognizes_amazon_and_manufacturers():
    assert is_trusted_seller("Amazon.co.jp")
    assert is_trusted_seller("アマゾンジャパン合同会社")
    assert is_trusted_seller("任天堂株式会社")
    assert is_trusted_seller("Nintendo")
    assert is_trusted_seller("タカラトミーモール")
    assert is_trusted_seller("LEGO Japan")


def test_is_trusted_seller_rejects_unknown_and_empty():
    assert not is_trusted_seller("")
    assert not is_trusted_seller("ゲームショップ ABC")
    assert not is_trusted_seller("RareBoxJP")


def test_is_low_stock_reseller_signal_detects_zanri_pattern():
    # B0G398BYV6 の実データ
    assert is_low_stock_reseller_signal("残り10点 ご注文はお早めに")
    assert is_low_stock_reseller_signal("残り 3 点")
    assert is_low_stock_reseller_signal("在庫わずか")


def test_is_low_stock_reseller_signal_passes_normal_availability():
    assert not is_low_stock_reseller_signal("")
    assert not is_low_stock_reseller_signal("在庫あり")
    assert not is_low_stock_reseller_signal("通常配送無料")


def test_check_no_reseller_pricing_fails_on_44pct_premium():
    """B0G398BYV6 の実データ再現: Amazon ¥8480 / Rakuten ¥5880 / Yahoo ¥6240."""
    article = {
        "product": {
            "asin": "B0G398BYV6",
            "prices": {
                "amazon": {"price": 8480},
                "rakuten": {"price": 5880},
                "yahoo": {"price": 6240},
            },
        }
    }
    result = check_no_reseller_pricing(article)
    assert not result.passed
    assert "reseller-pricing suspect" in result.message
    assert "1.44" in result.message  # 8480 / 5880 = 1.4422


def test_check_no_reseller_pricing_passes_on_normal_markup():
    """Amazon が 10% 高い程度なら通す (送料込み等の正常範囲)."""
    article = {
        "product": {
            "asin": "B0XXXXXX",
            "prices": {
                "amazon": {"price": 3300},
                "rakuten": {"price": 3000},
                "yahoo": {"price": 3100},
            },
        }
    }
    result = check_no_reseller_pricing(article)
    assert result.passed


def test_check_no_reseller_pricing_no_cross_prices_is_ok():
    """rakuten/yahoo データ無しなら無効化 (false-trip 防止)."""
    article = {
        "product": {
            "asin": "B0XXXXXX",
            "prices": {"amazon": {"price": 9999}},
        }
    }
    result = check_no_reseller_pricing(article)
    assert result.passed


def test_check_no_reseller_pricing_no_amazon_price_is_ok():
    article = {"product": {"prices": {"rakuten": {"price": 1000}}}}
    result = check_no_reseller_pricing(article)
    assert result.passed


def test_load_asin_blocklist_real_file():
    """data/asin_blocklist.json に B0G398BYV6 が登録済みであること."""
    blocklist = _load_asin_blocklist(str(ROOT.parent / "data" / "asin_blocklist.json"))
    assert "B0G398BYV6" in blocklist


def test_load_asin_blocklist_missing_returns_empty():
    assert _load_asin_blocklist("/nonexistent/path.json") == set()
