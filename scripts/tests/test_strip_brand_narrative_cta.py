"""Unit tests for strip_brand_narrative_cta.py (#5330).

Coverage:
1. strip_cta      - 表記ゆれ 2 種 / 改行入り / front matter 保護 / 冪等性
2. iter_index_files / main - --check の exit code、書き換え
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

import strip_brand_narrative_cta as sc  # noqa: E402

_CTA_ROBO = (
    "本サイトでは、おもちゃロボ独自の知育スコア "
    "(教育性 / 長寿命性 / 安全性 / コストパフォーマンスの 4 軸) で"
    "アガツマ商品を横断比較。お子さまの年齢にぴったりの 1 点を "
    "Amazon・楽天・Yahoo! の最安値で見つけられます。"
)
_CTA_EDITORIAL_WRAPPED = (
    "本サイトでは、編集部独自の知育スコア "
    "(教育性 / 長寿命性 / 安全性 / コストパフォーマンスの 4 軸) で\n"
    "レゴ商品を横断比較。お子さまの年齢にぴったりの 1 点を "
    "Amazon・楽天・Yahoo! の最安値で\n見つけられます。"
)


def _doc(cta: str, title: str = "アガツマ") -> str:
    return (
        f'---\ntitle: "{title}"\nauto_generated: true\n---\n\n'
        f"## {title}はなぜ定番なのか\n\n本文の段落その1。\n\n本文の段落その2。\n\n{cta}\n"
    )


class TestStripCta(unittest.TestCase):
    def test_strips_omocharobo_variant(self):
        out, changed = sc.strip_cta(_doc(_CTA_ROBO))
        self.assertTrue(changed)
        self.assertNotIn("横断比較", out)
        self.assertIn("本文の段落その2。", out)

    def test_strips_editorial_variant_with_newlines(self):
        out, changed = sc.strip_cta(_doc(_CTA_EDITORIAL_WRAPPED, "レゴ"))
        self.assertTrue(changed)
        self.assertNotIn("横断比較", out)

    def test_keeps_front_matter(self):
        out, _ = sc.strip_cta(_doc(_CTA_ROBO))
        self.assertTrue(out.startswith('---\ntitle: "アガツマ"'))
        self.assertIn("auto_generated: true", out)

    def test_idempotent(self):
        once, changed1 = sc.strip_cta(_doc(_CTA_ROBO))
        twice, changed2 = sc.strip_cta(once)
        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertEqual(once, twice)

    def test_no_cta_is_untouched(self):
        text = _doc("普通の締め段落です。")
        out, changed = sc.strip_cta(text)
        self.assertFalse(changed)
        self.assertEqual(out, text)

    def test_body_only_document(self):
        out, changed = sc.strip_cta(f"段落1。\n\n{_CTA_ROBO}\n")
        self.assertTrue(changed)
        self.assertEqual(out.strip(), "段落1。")

    def test_does_not_touch_front_matter_only_stub(self):
        text = '---\ntitle: "All Bright"\n---\n'
        out, changed = sc.strip_cta(text)
        self.assertFalse(changed)
        self.assertEqual(out, text)


class TestMain(unittest.TestCase):
    def _tree(self, root: pathlib.Path) -> None:
        (root / "agatsuma").mkdir(parents=True)
        (root / "agatsuma" / "_index.md").write_text(
            _doc(_CTA_ROBO), encoding="utf-8"
        )
        (root / "all-bright").mkdir(parents=True)
        (root / "all-bright" / "_index.md").write_text(
            '---\ntitle: "All Bright"\n---\n', encoding="utf-8"
        )

    def test_check_reports_and_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "brands"
            self._tree(root)
            rc = sc.main(["--brands-dir", str(root), "--check"])
            self.assertEqual(rc, 1)
            # --check は書き換えない
            self.assertIn(
                "横断比較",
                (root / "agatsuma" / "_index.md").read_text(encoding="utf-8"),
            )

    def test_strip_then_check_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "brands"
            self._tree(root)
            self.assertEqual(sc.main(["--brands-dir", str(root)]), 0)
            self.assertEqual(
                sc.main(["--brands-dir", str(root), "--check"]), 0
            )

    def test_missing_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "nope"
            self.assertEqual(sc.iter_index_files(missing), [])
            self.assertEqual(sc.main(["--brands-dir", str(missing)]), 0)


if __name__ == "__main__":
    unittest.main()
