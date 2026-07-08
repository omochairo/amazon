"""ensure_tag_slugs.py の単体テスト (#2817 Phase 4)。

カバレッジ:
1. _collect_tags_from_paths: 指定パスからの tags 収集・サイドカー/不存在/不正 JSON の skip
2. main: 新規用語のみ登録・既存は不変・brand_taxonomy は毎回全件スキャン・
   stdin 経由の受け渡し・--dry-run で書き込みしない
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from io import StringIO

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ensure_tag_slugs as ets  # noqa: E402
import yaml  # noqa: E402


class CollectTagsFromPathsTest(unittest.TestCase):
    def test_collects_and_dedups_excluding_sidecars_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            f1 = root / "2026-07-01-B0001.json"
            f1.write_text(json.dumps({"tags": ["レゴ", "ブロック", " レゴ "]}), encoding="utf-8")
            f2 = root / "2026-07-01-B0001.quality.json"
            f2.write_text(json.dumps({"tags": ["除外されるべき"]}), encoding="utf-8")
            missing = root / "no-such-file.json"
            broken = root / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            tags = ets._collect_tags_from_paths(
                [str(f1), str(f2), str(missing), str(broken), ""]
            )
            self.assertEqual(tags, {"レゴ", "ブロック"})

    def test_empty_paths_returns_empty_set(self):
        self.assertEqual(ets._collect_tags_from_paths([]), set())


class MainTest(unittest.TestCase):
    def _run(self, argv):
        argv_backup = sys.argv
        try:
            sys.argv = argv
            return ets.main()
        finally:
            sys.argv = argv_backup

    def test_registers_only_new_terms_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            article = root / "2026-07-08-B0999.json"
            article.write_text(json.dumps({"tags": ["新用語", "既存タグ"]}), encoding="utf-8")

            slugs = root / "term_slugs.yaml"
            slugs.write_text("既存タグ: legacy-kept\n", encoding="utf-8")

            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text("brands: []\n", encoding="utf-8")

            rc = self._run([
                "ensure_tag_slugs.py",
                "--articles", str(article),
                "--slugs", str(slugs),
                "--brand-taxonomy", str(taxonomy),
            ])
            self.assertEqual(rc, 0)
            result = yaml.safe_load(slugs.read_text(encoding="utf-8"))
            self.assertEqual(result["既存タグ"], "legacy-kept")
            self.assertIn("新用語", result)

    def test_brand_taxonomy_always_fully_rescanned(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            slugs = root / "term_slugs.yaml"
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text(
                "brands:\n  - canonical: 新ブランド\n    aliases: [新ブランド(NEWBRAND)]\n",
                encoding="utf-8",
            )
            rc = self._run([
                "ensure_tag_slugs.py",
                "--articles",
                "--slugs", str(slugs),
                "--brand-taxonomy", str(taxonomy),
            ])
            self.assertEqual(rc, 0)
            result = yaml.safe_load(slugs.read_text(encoding="utf-8"))
            self.assertEqual(result["新ブランド"], "newbrand")

    def test_no_new_terms_does_not_touch_output_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            slugs = root / "term_slugs.yaml"
            slugs.write_text("既存タグ: legacy-kept\n", encoding="utf-8")
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text("brands: []\n", encoding="utf-8")
            before = slugs.read_text(encoding="utf-8")

            rc = self._run([
                "ensure_tag_slugs.py",
                "--articles",
                "--slugs", str(slugs),
                "--brand-taxonomy", str(taxonomy),
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(slugs.read_text(encoding="utf-8"), before)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            article = root / "2026-07-08-B0999.json"
            article.write_text(json.dumps({"tags": ["新用語"]}), encoding="utf-8")
            slugs = root / "term_slugs.yaml"
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text("brands: []\n", encoding="utf-8")

            rc = self._run([
                "ensure_tag_slugs.py",
                "--articles", str(article),
                "--slugs", str(slugs),
                "--brand-taxonomy", str(taxonomy),
                "--dry-run",
            ])
            self.assertEqual(rc, 0)
            self.assertFalse(slugs.exists())

    def test_reads_article_paths_from_stdin_when_no_flag_given(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            article = root / "2026-07-08-B0999.json"
            article.write_text(json.dumps({"tags": ["新用語"]}), encoding="utf-8")
            slugs = root / "term_slugs.yaml"
            taxonomy = root / "brand_taxonomy.yaml"
            taxonomy.write_text("brands: []\n", encoding="utf-8")

            argv_backup, stdin_backup = sys.argv, sys.stdin
            try:
                sys.argv = [
                    "ensure_tag_slugs.py",
                    "--slugs", str(slugs),
                    "--brand-taxonomy", str(taxonomy),
                ]
                sys.stdin = StringIO(f"{article}\n")
                rc = ets.main()
            finally:
                sys.argv, sys.stdin = argv_backup, stdin_backup

            self.assertEqual(rc, 0)
            result = yaml.safe_load(slugs.read_text(encoding="utf-8"))
            self.assertIn("新用語", result)


if __name__ == "__main__":
    unittest.main()
