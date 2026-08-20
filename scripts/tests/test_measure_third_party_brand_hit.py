"""Unit tests for measure_third_party_brand_hit (#1600 Phase 2 の効果測定).

ネットワークも per_asin 実データも使わず、トークン抽出と集計の規約だけを固定する。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import measure_third_party_brand_hit as M  # noqa: E402


class BrandTokenTest(unittest.TestCase):
    def test_leading_latin_token_is_taken(self):
        self.assertEqual(M.brand_tokens("LEGO クラシック 黄色のアイデアボックス"), ["LEGO"])
        self.assertEqual(M.brand_tokens("Ed-Inter 木のおもちゃ"), ["Ed-Inter"])

    def test_generic_words_are_skipped(self):
        # 先頭が装飾語でも、その後ろのブランド名を拾う。
        self.assertEqual(M.brand_tokens("NEW Hape 木製 パズル"), ["Hape"])
        self.assertEqual(M.brand_tokens("Toy ボーネルンド BorneLund"), ["BorneLund"])

    def test_model_numbers_are_not_brands(self):
        # 英字 2 文字 + 数字は型番。ブランド名として採らない。
        self.assertEqual(M.brand_tokens("くもん出版 立体ドット絵メーカー RD-10"), [])
        self.assertEqual(M.brand_tokens("SC-500 電子ブロック"), [])

    def test_no_latin_token_yields_empty(self):
        self.assertEqual(M.brand_tokens("知育玩具 木製 パズル 3歳"), [])
        self.assertEqual(M.brand_tokens(""), [])


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _asin(self, asin, title, sources=None):
        d = self.base / asin
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "amazon.json", "w", encoding="utf-8") as f:
            json.dump({"item": {"asin": asin, "title": title}}, f)
        if sources is not None:
            with open(d / M._f.OUT_NAME, "w", encoding="utf-8") as f:
                json.dump({"asin": asin, "sources": sources}, f)

    def test_hit_when_brand_appears_in_title_or_snippet(self):
        self._asin("B0AAAAAAA1", "Hape 木製パズル", [
            {"title": "hape のパズルを試した", "snippet": "感想", "url": "https://x.example/1"},
        ])
        m = M.measure(["B0AAAAAAA1"], self.base, with_url=False)
        self.assertEqual((m["fetched"], m["hits"]), (1, 1))
        self.assertEqual(m["rate_all"], 100.0)

    def test_miss_when_only_category_talk(self):
        self._asin("B0AAAAAAA2", "Hape 木製パズル", [
            {"title": "木製パズルのおすすめ 10 選", "snippet": "知育玩具",
             "url": "https://x.example/2"},
        ])
        m = M.measure(["B0AAAAAAA2"], self.base, with_url=False)
        self.assertEqual((m["fetched"], m["hits"]), (1, 0))

    def test_url_field_is_opt_in(self):
        # スラッグにブランド名が入るぶん url を含めると率が上がる。既定では見ない。
        self._asin("B0AAAAAAA3", "Hape 木製パズル", [
            {"title": "木製パズル比較", "snippet": "", "url": "https://x.example/hape-review"},
        ])
        self.assertEqual(M.measure(["B0AAAAAAA3"], self.base, with_url=False)["hits"], 0)
        self.assertEqual(M.measure(["B0AAAAAAA3"], self.base, with_url=True)["hits"], 1)

    def test_unfetched_asin_is_not_in_denominator(self):
        self._asin("B0AAAAAAA4", "Hape 木製パズル")  # third_party_sources.json 無し
        m = M.measure(["B0AAAAAAA4"], self.base, with_url=False)
        self.assertEqual(m["fetched"], 0)

    def test_two_denominators(self):
        # トークンが取れない ASIN は「全体分母」にだけ入り、miss として数える。
        self._asin("B0AAAAAAA5", "Hape 木製パズル", [
            {"title": "hape レビュー", "snippet": "", "url": ""}])
        self._asin("B0AAAAAAA6", "知育玩具 木製 パズル", [
            {"title": "木製パズル特集", "snippet": "", "url": ""}])
        m = M.measure(["B0AAAAAAA5", "B0AAAAAAA6"], self.base, with_url=False)
        self.assertEqual(m["fetched"], 2)
        self.assertEqual(m["with_brand_token"], 1)
        self.assertEqual(m["hits"], 1)
        self.assertEqual(m["rate_all"], 50.0)
        self.assertEqual(m["rate_with_token"], 100.0)


if __name__ == "__main__":
    unittest.main()
