"""Unit tests for filter_raw_per_asin.extract_product_terms (#8162 案D)。

_JA_TERM は元々 {3,12} で 13 字以上の連続を 12 字 + 残りに分割していた。
分割された断片は動画タイトルと部分一致しにくく、商品同定シグナルとして
機能しなかった (#8162 問題3)。上限を撤廃し、13 字以上でも 1 語として
扱えることを確認する。
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import filter_raw_per_asin as F  # noqa: E402


class ExtractProductTermsLongTermTest(unittest.TestCase):
    def test_13_chars_not_split(self) -> None:
        title = "BabyBusミュウミュウおすわりぬいぐるみ"
        brands, series = F.extract_brand_series(title)
        terms = F.extract_product_terms(title, brands, series)
        self.assertIn("ミュウミュウおすわりぬいぐるみ", terms)
        # 旧実装が生成していた断片 (12字 + 残り) が残っていないこと
        self.assertNotIn("ミュウミュウおすわりぬい", terms)
        self.assertNotIn("ぐるみ", terms)

    def test_14_chars_not_split(self) -> None:
        title = "ルービックキューブピクセル"
        brands, series = F.extract_brand_series(title)
        terms = F.extract_product_terms(title, brands, series)
        self.assertIn("ルービックキューブピクセル", terms)
        self.assertNotIn("ル", terms)


if __name__ == "__main__":
    unittest.main()
