"""Unit tests for fetch_books query strategy + NG filter (#503 / #560).

Covers:
- ``BRAND_GENRE_VOCAB`` maps known brands to the right secondary keyword
- ``build_query_chain`` produces fallback chain (primary + brand-only)
- ``is_books_noise`` rejects travel guides, year-edition books, and
  adult-targeted exam books that historically slipped through the
  strict per-ASIN filter.
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from fetch_books import (  # noqa: E402
    BRAND_GENRE_VOCAB,
    build_query_chain,
    extract_brand,
    is_books_noise,
)


class BrandGenreVocabTest(unittest.TestCase):
    def test_character_brand_maps_to_ehon(self):
        self.assertEqual(BRAND_GENRE_VOCAB["アンパンマン"], "絵本")
        self.assertEqual(BRAND_GENRE_VOCAB["ポケモン"], "絵本")
        self.assertEqual(BRAND_GENRE_VOCAB["シルバニアファミリー"], "絵本")

    def test_vehicle_brand_maps_to_norimono(self):
        self.assertEqual(BRAND_GENRE_VOCAB["プラレール"], "のりもの")
        self.assertEqual(BRAND_GENRE_VOCAB["トミカ"], "のりもの")

    def test_stem_brand_maps_to_zukan(self):
        self.assertEqual(BRAND_GENRE_VOCAB["レゴ"], "図鑑")
        self.assertEqual(BRAND_GENRE_VOCAB["学研"], "図鑑")
        self.assertEqual(BRAND_GENRE_VOCAB["ボーネルンド"], "図鑑")

    def test_kumon_maps_to_drill(self):
        self.assertEqual(BRAND_GENRE_VOCAB["くもん"], "ドリル")
        self.assertEqual(BRAND_GENRE_VOCAB["公文"], "ドリル")


class BuildQueryChainTest(unittest.TestCase):
    def test_known_brand_yields_primary_plus_brand_only(self):
        chain = build_query_chain("プラレール", "")
        self.assertEqual(chain, ["プラレール のりもの", "プラレール"])

    def test_unknown_brand_defaults_to_ehon(self):
        chain = build_query_chain("UnknownBrand", "")
        self.assertEqual(chain, ["UnknownBrand 絵本", "UnknownBrand"])

    def test_no_brand_with_fallback_kw(self):
        chain = build_query_chain("", "ジグソーパズル")
        self.assertEqual(chain, ["ジグソーパズル 絵本"])

    def test_no_brand_no_fallback_returns_empty(self):
        self.assertEqual(build_query_chain("", ""), [])

    def test_lego_uses_zukan_first(self):
        chain = build_query_chain("レゴ", "")
        self.assertEqual(chain[0], "レゴ 図鑑")
        self.assertIn("レゴ", chain)

    def test_chain_dedupes_when_primary_equals_brand_only(self):
        # Shouldn't happen in practice but exercise the dedup guard.
        chain = build_query_chain("X", "")
        self.assertEqual(len(chain), len(set(chain)))


class IsBooksNoiseTest(unittest.TestCase):
    def test_rurubu_is_noise(self):
        # #560 の代表例: 「ジグソーパズル → るるぶこどもとあそぼ」
        self.assertTrue(is_books_noise("るるぶこどもとあそぼ！首都圏'15～'16"))

    def test_mapple_is_noise(self):
        self.assertTrue(is_books_noise("まっぷるキッズ 関東"))

    def test_year_edition_pattern_noise(self):
        self.assertTrue(is_books_noise("'15～'16 おでかけガイド"))
        self.assertTrue(is_books_noise("2024～2025 年版おでかけブック"))
        self.assertTrue(is_books_noise("2024年版こども年鑑"))

    def test_adult_exam_book_noise(self):
        self.assertTrue(is_books_noise("公務員試験 数的推理"))
        self.assertTrue(is_books_noise("FP3級 教科書"))

    def test_travel_guide_keywords_noise(self):
        self.assertTrue(is_books_noise("東京 観光ガイド"))
        self.assertTrue(is_books_noise("地球の歩き方 ヨーロッパ"))

    def test_legitimate_kids_book_passes(self):
        self.assertFalse(is_books_noise("アンパンマンの絵本"))
        self.assertFalse(is_books_noise("プラレール大図鑑"))
        self.assertFalse(is_books_noise("くもんのこどもドリル"))
        self.assertFalse(is_books_noise(""))
        self.assertFalse(is_books_noise(None))


class ExtractBrandSanityTest(unittest.TestCase):
    def test_extract_brand_still_works(self):
        self.assertEqual(extract_brand("プラレール スーパー絵本セット"), "プラレール")
        self.assertEqual(extract_brand("BEVERLY 108ピースジグソーパズル"), "")


if __name__ == "__main__":
    unittest.main()
