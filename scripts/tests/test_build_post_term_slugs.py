"""_frontmatter_meta の tags/brands スラッグ変換テスト (#2817 Phase 2)。

data/articles/*.json (JP) は不変のまま、Hugo content 生成時にだけ
tags/brands を英語スラッグへ変換することを検証する。

build_post._TERM_SLUGS はモジュールロード時に本番 data/term_slugs.yaml を読む
シングルトンのため、テストでは独立した temp ファイル (空マッピング) に差し替えて
本番データの現在の割当状態 (例: 既存の "LEGO"/"レゴ" 表記ゆれ衝突) に左右されない
決定的な結果にする。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_post  # type: ignore[import-not-found]  # noqa: E402
from build_post import _frontmatter_meta, _term_to_slug  # type: ignore[import-not-found]  # noqa: E402
from term_slug import TermSlugMap  # type: ignore[import-not-found]  # noqa: E402

_TEST_BRAND_TAXONOMY = """
brands:
  - canonical: レゴ
    aliases: [レゴ(LEGO), LEGO, レゴジャパン]
    tier: S
"""


def _article(tags=None, brand=None):
    return {
        "slug": "2026-01-01-B0TEST00001",
        "title": "テスト商品",
        "product": {"asin": "B0TEST00001", "brand": brand or "", "name": "テスト商品"},
        "tags": tags or [],
    }


class TermToSlugTest(unittest.TestCase):
    def setUp(self):
        self._orig = build_post._TERM_SLUGS
        self._td = tempfile.TemporaryDirectory()
        td = pathlib.Path(self._td.name)
        taxonomy = td / "brand_taxonomy.yaml"
        taxonomy.write_text(_TEST_BRAND_TAXONOMY, encoding="utf-8")
        build_post._TERM_SLUGS = TermSlugMap(
            path=td / "term_slugs.yaml", brand_taxonomy_path=taxonomy
        )

    def tearDown(self):
        build_post._TERM_SLUGS = self._orig
        self._td.cleanup()

    def test_known_brand_uses_curated_alias(self):
        self.assertEqual(_term_to_slug("レゴ"), "lego")

    def test_unknown_term_still_returns_stable_slug(self):
        slug = _term_to_slug("完全に新規のタグ用語XYZ")
        self.assertTrue(slug)
        self.assertEqual(slug, _term_to_slug("完全に新規のタグ用語XYZ"))  # 同一呼び出しで安定


class FrontmatterMetaTagsBrandsTest(unittest.TestCase):
    def setUp(self):
        self._orig = build_post._TERM_SLUGS
        self._td = tempfile.TemporaryDirectory()
        td = pathlib.Path(self._td.name)
        taxonomy = td / "brand_taxonomy.yaml"
        taxonomy.write_text(_TEST_BRAND_TAXONOMY, encoding="utf-8")
        build_post._TERM_SLUGS = TermSlugMap(
            path=td / "term_slugs.yaml", brand_taxonomy_path=taxonomy
        )

    def tearDown(self):
        build_post._TERM_SLUGS = self._orig
        self._td.cleanup()

    def _meta_for(self, data):
        with tempfile.TemporaryDirectory() as td:
            json_path = pathlib.Path(td) / "article.json"
            json_path.write_text("{}", encoding="utf-8")
            return _frontmatter_meta(data, data["slug"], draft=False, git_history={}, json_file_path=json_path)

    def test_tags_are_converted_to_english_slugs(self):
        data = _article(tags=["レゴ", "知育玩具"])
        meta = self._meta_for(data)
        self.assertEqual(meta["tags"], ["lego", "chiikugangu"])

    def test_brand_field_stays_japanese_canonical(self):
        data = _article(brand="レゴジャパン")  # brand_taxonomy.yaml の alias 経由で正規化
        meta = self._meta_for(data)
        self.assertEqual(meta["brand"], "レゴ")  # 表示/構造化データ用は JP のまま

    def test_brands_taxonomy_field_uses_english_slug(self):
        data = _article(brand="レゴジャパン")
        meta = self._meta_for(data)
        self.assertEqual(meta["brands"], ["lego"])

    def test_tag_and_brand_of_same_concept_share_one_slug(self):
        # #2701 P3 (is_brand_tag.html) の前提: 同一概念の tag と brand canonical は
        # 同じスラッグを共有し続けなければならない (移行後も #.Title 一致判定が機能する)。
        data = _article(tags=["レゴ"], brand="レゴジャパン")
        meta = self._meta_for(data)
        self.assertEqual(meta["tags"], ["lego"])
        self.assertEqual(meta["brands"], ["lego"])


if __name__ == "__main__":
    unittest.main()
