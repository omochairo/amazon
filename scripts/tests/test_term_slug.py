"""term_slug.py の単体テスト (#2817 Phase 1)。

カバレッジ:
1. _is_ascii / _slugify: 基本変換
2. _load_brand_ascii_aliases: canonical 自身と対を成す括弧併記のみ採用し、
   傘下シリーズ名 (例: タカラトミーの "ゾイド"/"ZOIDS") を誤って拾わない
3. TermSlugMap: 既存エントリの再利用 (冪等) / 新規割当 / 永続化 (save/load) / 衝突時の連番
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import term_slug as ts  # noqa: E402

_SAMPLE_BRAND_TAXONOMY = """
brands:
  - canonical: レゴ
    aliases: [レゴ(LEGO), LEGO, レゴジャパン]
    tier: S
  - canonical: タカラトミー
    aliases: [タカラトミー(TAKARA TOMY), TAKARA TOMY, トミカ, TOMICA, ゾイド, ZOIDS]
    tier: S
  - canonical: ノーブランド代表
    aliases: [代表エイリアス]
    tier: D
"""


class SlugifyTest(unittest.TestCase):
    def test_is_ascii(self):
        self.assertTrue(ts._is_ascii("LEGO Education"))
        self.assertFalse(ts._is_ascii("レゴ"))

    def test_slugify_basic(self):
        self.assertEqual(ts._slugify("TAKARA TOMY"), "takara-tomy")
        self.assertEqual(ts._slugify("LEGO Education"), "lego-education")
        self.assertEqual(ts._slugify("  A -- B  "), "a-b")


class BrandAsciiAliasTest(unittest.TestCase):
    def _write_taxonomy(self, td):
        p = pathlib.Path(td) / "brand_taxonomy.yaml"
        p.write_text(_SAMPLE_BRAND_TAXONOMY, encoding="utf-8")
        return p

    def test_extracts_paired_ascii_form(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_taxonomy(td)
            aliases = ts._load_brand_ascii_aliases(p)
            self.assertEqual(aliases["レゴ"], "LEGO")

    def test_does_not_pick_unrelated_subline_alias(self):
        # #2817: タカラトミーの傘下シリーズ "ゾイド"/"ZOIDS" を誤って拾わない
        # (実バグ: 単純に「候補の中で最短の ASCII」を選ぶと ZOIDS が勝ってしまっていた)
        with tempfile.TemporaryDirectory() as td:
            p = self._write_taxonomy(td)
            aliases = ts._load_brand_ascii_aliases(p)
            self.assertEqual(aliases["タカラトミー"], "TAKARA TOMY")

    def test_no_paired_form_and_non_ascii_canonical_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_taxonomy(td)
            aliases = ts._load_brand_ascii_aliases(p)
            self.assertNotIn("ノーブランド代表", aliases)

    def test_missing_file_returns_empty(self):
        self.assertEqual(ts._load_brand_ascii_aliases(pathlib.Path("/no/such.yaml")), {})


class TermSlugMapTest(unittest.TestCase):
    def _make_map(self, td):
        taxonomy = self._write_taxonomy(td)
        return ts.TermSlugMap(
            path=pathlib.Path(td) / "term_slugs.yaml",
            brand_taxonomy_path=taxonomy,
        )

    def _write_taxonomy(self, td):
        p = pathlib.Path(td) / "brand_taxonomy.yaml"
        p.write_text(_SAMPLE_BRAND_TAXONOMY, encoding="utf-8")
        return p

    def test_prefers_brand_alias_over_romanization(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            self.assertEqual(m.get_or_create("レゴ"), "lego")

    def test_ascii_term_slugified_as_is(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            self.assertEqual(m.get_or_create("Snap Circuits"), "snap-circuits")

    def test_idempotent_get_or_create(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            first = m.get_or_create("レゴ")
            second = m.get_or_create("レゴ")
            self.assertEqual(first, second)

    def test_save_and_reload_preserves_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            m.get_or_create("レゴ")
            m.save()
            m2 = ts.TermSlugMap(
                path=pathlib.Path(td) / "term_slugs.yaml",
                brand_taxonomy_path=pathlib.Path(td) / "brand_taxonomy.yaml",
            )
            self.assertEqual(m2.get("レゴ"), "lego")

    def test_reload_does_not_recompute_existing_slug(self):
        # 既存エントリは save/reload をまたいでも再計算されない (安定性の要)
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "term_slugs.yaml"
            path.write_text("レゴ: custom-legacy-slug\n", encoding="utf-8")
            m = ts.TermSlugMap(path=path, brand_taxonomy_path=pathlib.Path(td) / "brand_taxonomy.yaml")
            self.assertEqual(m.get_or_create("レゴ"), "custom-legacy-slug")

    def test_collision_gets_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            first = m.get_or_create("Snap Circuits")
            second = m.get_or_create("Snap  Circuits")  # 異なる原文字列・同じ base slug
            self.assertEqual(first, "snap-circuits")
            self.assertEqual(second, "snap-circuits-2")

    def test_get_returns_none_for_unknown_term(self):
        with tempfile.TemporaryDirectory() as td:
            m = self._make_map(td)
            self.assertIsNone(m.get("未登録タグ"))


if __name__ == "__main__":
    unittest.main()
