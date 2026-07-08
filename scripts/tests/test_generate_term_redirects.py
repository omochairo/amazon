"""generate_term_redirects.py の単体テスト (#2817 Phase 3)。

カバレッジ:
1. _indexed_slugs_by_kind: sitemap.xml から /tags/ /brands/ の slug 抽出
   (一覧ページ自体は除外)
2. _reverse_slug_map: slug -> JP用語 の逆引き
3. build_redirect_lines: 生UTF-8 + percent-encoded の両形式を出力、
   term==slug (実質変化なし) は redirect 対象外
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_term_redirects as gtr  # noqa: E402

_SAMPLE_SITEMAP = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://navi.omcha.jp/tags/</loc></url>
<url><loc>https://navi.omcha.jp/tags/lego/</loc></url>
<url><loc>https://navi.omcha.jp/tags/anpanman/</loc></url>
<url><loc>https://navi.omcha.jp/brands/</loc></url>
<url><loc>https://navi.omcha.jp/brands/lego/</loc></url>
<url><loc>https://navi.omcha.jp/products/b0test00001/</loc></url>
</urlset>
"""


class IndexedSlugsTest(unittest.TestCase):
    def test_extracts_tags_and_brands_excluding_list_pages(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "sitemap.xml"
            p.write_text(_SAMPLE_SITEMAP, encoding="utf-8")
            result = gtr._indexed_slugs_by_kind(p)
            self.assertEqual(result["tags"], {"lego", "anpanman"})
            self.assertEqual(result["brands"], {"lego"})

    def test_ignores_non_term_urls(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "sitemap.xml"
            p.write_text(_SAMPLE_SITEMAP, encoding="utf-8")
            result = gtr._indexed_slugs_by_kind(p)
            self.assertNotIn("b0test00001", result["tags"] | result["brands"])


class ReverseSlugMapTest(unittest.TestCase):
    def test_builds_slug_to_term_map(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "term_slugs.yaml"
            p.write_text("レゴ: lego\nアンパンマン: anpanman\n", encoding="utf-8")
            result = gtr._reverse_slug_map(p)
            self.assertEqual(result, {"lego": "レゴ", "anpanman": "アンパンマン"})


class BuildRedirectLinesTest(unittest.TestCase):
    def test_emits_raw_and_percent_encoded_forms(self):
        indexed = {"tags": {"lego"}, "brands": set()}
        reverse = {"lego": "レゴ"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertIn("/tags/レゴ/ /tags/lego/ 301", lines)
        self.assertIn("/tags/%E3%83%AC%E3%82%B4/ /tags/lego/ 301", lines)
        self.assertEqual(len(lines), 2)

    def test_skips_when_term_equals_slug(self):
        # 実質変化なし (例: 元から ASCII で slug と同一) は redirect 不要
        indexed = {"tags": {"stem"}, "brands": set()}
        reverse = {"stem": "stem"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, [])

    def test_skips_when_slug_missing_from_reverse_map(self):
        indexed = {"tags": {"unknown-slug"}, "brands": set()}
        reverse = {}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertEqual(lines, [])

    def test_brands_and_tags_use_correct_path_prefix(self):
        indexed = {"tags": set(), "brands": {"lego"}}
        reverse = {"lego": "レゴ"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        self.assertIn("/brands/レゴ/ /brands/lego/ 301", lines)

    def test_ascii_case_change_does_not_double_encode_identically(self):
        # percent-encode した結果が生の形式と同一 (=元々 ASCII) なら重複行を出さない
        indexed = {"tags": {"lotus-life"}, "brands": set()}
        reverse = {"lotus-life": "LOTUS LIFE"}
        lines = gtr.build_redirect_lines(indexed, reverse)
        # スペースは quote() で %20 になるため生形式と percent-encoded 形式は異なる → 2行
        self.assertEqual(len(lines), 2)
        self.assertIn("/tags/LOTUS LIFE/ /tags/lotus-life/ 301", lines)
        self.assertIn("/tags/LOTUS%20LIFE/ /tags/lotus-life/ 301", lines)


if __name__ == "__main__":
    unittest.main()
