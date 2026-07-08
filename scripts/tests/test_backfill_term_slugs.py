"""backfill_term_slugs.py の単体テスト (#2817 Phase 1)。

カバレッジ:
1. _collect_tags: 記事 tags の収集・サイドカー除外・空/不正 JSON の skip
2. _collect_brand_canonicals: brand_taxonomy.yaml の canonical 一覧抽出
3. main: 初回生成が冪等 (再実行で既存エントリを変えない) であること
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

import backfill_term_slugs as bts  # noqa: E402
import yaml  # noqa: E402


class CollectTagsTest(unittest.TestCase):
    def test_collects_and_dedups_tags_excluding_sidecars(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "2026-05-01-B0001.json").write_text(
                json.dumps({"tags": ["レゴ", "ブロック", " レゴ "]}), encoding="utf-8"
            )
            (root / "2026-05-02-B0002.json").write_text(
                json.dumps({"tags": ["知育玩具", "レゴ"]}), encoding="utf-8"
            )
            (root / "2026-05-01-B0001.quality.json").write_text(
                json.dumps({"tags": ["除外されるべき"]}), encoding="utf-8"
            )
            (root / "2026-05-03-B0003.json").write_text("{not json", encoding="utf-8")
            tags = bts._collect_tags(str(root))
            self.assertEqual(tags, {"レゴ", "ブロック", "知育玩具"})

    def test_empty_dir_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(bts._collect_tags(td), set())


class CollectBrandCanonicalsTest(unittest.TestCase):
    def test_extracts_canonicals(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "brand_taxonomy.yaml"
            p.write_text(
                "brands:\n  - canonical: レゴ\n    aliases: [LEGO]\n"
                "  - canonical: バンダイ\n    aliases: [BANDAI]\n",
                encoding="utf-8",
            )
            self.assertEqual(bts._collect_brand_canonicals(str(p)), {"レゴ", "バンダイ"})

    def test_missing_file_returns_empty(self):
        self.assertEqual(bts._collect_brand_canonicals("/no/such.yaml"), set())


class BrandPriorityOrderingTest(unittest.TestCase):
    def test_brand_canonical_claims_slug_before_colliding_raw_tag(self):
        # 実データで発見された事故: "LEGO" (生タグ・表記ゆれ) と "レゴ" (brand
        # canonical) は同じ base slug "lego" を狙うため、処理順序次第でどちらかが
        # "-2" に回る。価値の高いブランドハブ側 ("レゴ") が素の "lego" を
        # 取れなければならない。
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            articles = root / "articles"
            articles.mkdir()
            (articles / "2026-05-01-B0001.json").write_text(
                json.dumps({"tags": ["LEGO", "レゴ"]}), encoding="utf-8"
            )
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text(
                "brands:\n  - canonical: レゴ\n    aliases: [レゴ(LEGO), LEGO]\n",
                encoding="utf-8",
            )
            out = root / "term_slugs.yaml"
            argv_backup = sys.argv
            try:
                sys.argv = [
                    "backfill_term_slugs.py",
                    "--articles-dir", str(articles),
                    "--brand-taxonomy", str(taxonomy),
                    "--out", str(out),
                ]
                self.assertEqual(bts.main(), 0)
                result = yaml.safe_load(out.read_text(encoding="utf-8"))
                self.assertEqual(result["レゴ"], "lego")
                self.assertEqual(result["LEGO"], "lego-2")
            finally:
                sys.argv = argv_backup


class MainIdempotencyTest(unittest.TestCase):
    def test_rerun_preserves_existing_slugs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            articles = root / "articles"
            articles.mkdir()
            (articles / "2026-05-01-B0001.json").write_text(
                json.dumps({"tags": ["レゴ"]}), encoding="utf-8"
            )
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text(
                "brands:\n  - canonical: レゴ\n    aliases: [レゴ(LEGO), LEGO]\n",
                encoding="utf-8",
            )
            out = root / "term_slugs.yaml"

            argv_backup = sys.argv
            try:
                sys.argv = [
                    "backfill_term_slugs.py",
                    "--articles-dir", str(articles),
                    "--brand-taxonomy", str(taxonomy),
                    "--out", str(out),
                ]
                self.assertEqual(bts.main(), 0)
                first = yaml.safe_load(out.read_text(encoding="utf-8"))
                self.assertEqual(first["レゴ"], "lego")

                # 手動で既存スラッグを書き換えてから再実行 → 上書きされないこと
                out.write_text("レゴ: legacy-slug-kept\n", encoding="utf-8")
                self.assertEqual(bts.main(), 0)
                second = yaml.safe_load(out.read_text(encoding="utf-8"))
                self.assertEqual(second["レゴ"], "legacy-slug-kept")
            finally:
                sys.argv = argv_backup


if __name__ == "__main__":
    unittest.main()
