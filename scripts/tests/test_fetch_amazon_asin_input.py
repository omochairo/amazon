"""``--asin`` 入力の検証 (run #388 の事故)。

workflow_dispatch の「カンマ区切りASIN」欄に**検索キーワード**が入力され、
Creator API が ``1 validation error detected ... at 'itemIds'`` を返して
sniper が ``sys.exit(1)``。ステップは 1 秒で落ち、ログを深く辿らないと
「キーワードを ASIN 欄に入れた」ことが分からなかった。API に投げる前に
弾いて、何を直せばよいかログに出す。
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_amazon  # noqa: E402


class ParseAsinCsvTest(unittest.TestCase):
    def test_accepts_asin_and_isbn10(self):
        valid, invalid = fetch_amazon._parse_asin_csv("B0GJX8ZWCK,4062762153,088888888X")
        self.assertEqual(valid, ["B0GJX8ZWCK", "4062762153", "088888888X"])
        self.assertEqual(invalid, [])

    def test_normalizes_case_and_whitespace(self):
        valid, invalid = fetch_amazon._parse_asin_csv(" b0gjx8zwck , B0BD84ZGKV ")
        self.assertEqual(valid, ["B0GJX8ZWCK", "B0BD84ZGKV"])
        self.assertEqual(invalid, [])

    def test_skips_empty_fields(self):
        valid, invalid = fetch_amazon._parse_asin_csv("B0GJX8ZWCK,,  ,")
        self.assertEqual(valid, ["B0GJX8ZWCK"])
        self.assertEqual(invalid, [])

    def test_empty_input(self):
        self.assertEqual(fetch_amazon._parse_asin_csv(""), ([], []))

    def test_rejects_run388_keywords(self):
        raw = ("ちいかわ,UniPro,スクイーズ,キラ★ガチャシール,オーボールラトル,"
               "めりー,シルバニアファミリー,ひらがじゃん,ハンドスピナー,すごろくや")
        valid, invalid = fetch_amazon._parse_asin_csv(raw)
        self.assertEqual(valid, [])
        self.assertEqual(len(invalid), 10)
        # 元の表記のままログに出す (どれを直せばよいか分かるように)
        self.assertIn("ちいかわ", invalid)
        self.assertIn("キラ★ガチャシール", invalid)

    def test_rejects_wrong_length_and_shape(self):
        raw = "B0GJX8ZWC,B0GJX8ZWCKK,0B0GJX8ZWC,B0GJX8-WCK"
        valid, invalid = fetch_amazon._parse_asin_csv(raw)
        self.assertEqual(valid, [])
        self.assertEqual(len(invalid), 4)

    def test_partially_valid_input_reports_only_the_bad_ones(self):
        valid, invalid = fetch_amazon._parse_asin_csv("B0GJX8ZWCK,ちいかわ")
        self.assertEqual(valid, ["B0GJX8ZWCK"])
        self.assertEqual(invalid, ["ちいかわ"])


if __name__ == "__main__":
    unittest.main()
