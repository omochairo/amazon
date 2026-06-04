"""select_sns_copy_targets.py の選定/store 除外ロジックのテスト (#1580 Part2)。

実行: amazon-clone 直下から `python -m unittest scripts.tests.test_select_sns_copy_targets`
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pick_sns_target as pst  # noqa: E402
import sns_copy_store as store  # noqa: E402
import select_sns_copy_targets as sel  # noqa: E402


def _article(asin, edu_domains=None, best_price=3000, date="2026-06-01", title=None):
    return {
        "slug": f"{date}-{asin}",
        "title": title or f"商品 {asin} のレビュー",
        "meta_description": "説明",
        "product": {
            "asin": asin,
            "name": f"商品 {asin}",
            "brand": "レゴ",
            "edu_domains": edu_domains if edu_domains is not None else ["STEM", "想像"],
            "best_price": best_price,
        },
    }


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self._art = tempfile.TemporaryDirectory()
        self._store = tempfile.TemporaryDirectory()
        self._state = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self._state.close()
        self._orig = (pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR)
        pst.ARTICLES_DIR = pathlib.Path(self._art.name)
        pst.STATE_FILE = pathlib.Path(self._state.name)
        store.STORE_DIR = pathlib.Path(self._store.name)
        # state を空 published で初期化
        pst.STATE_FILE.write_text('{"published": [], "updated": null}', encoding="utf-8")

    def tearDown(self):
        pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR = self._orig
        self._art.cleanup()
        self._store.cleanup()
        os.unlink(self._state.name)

    def _write_article(self, name, article):
        (pathlib.Path(self._art.name) / name).write_text(
            json.dumps(article, ensure_ascii=False), encoding="utf-8")

    def _write_store(self, asin, payload):
        (pathlib.Path(self._store.name) / f"{asin}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_top_n_respects_ranking_order(self):
        self._write_article("2026-06-01-B000000001.json",
                             _article("B000000001", edu_domains=["STEM", "言語", "運動", "想像"], best_price=15000))
        self._write_article("2026-06-01-B000000002.json",
                             _article("B000000002", edu_domains=[], best_price=800))
        out = sel.select_targets(top=5)
        self.assertEqual([t["asin"] for t in out], ["B000000001", "B000000002"])
        self.assertEqual(out[0]["title"], "商品 B000000001 のレビュー")
        self.assertEqual(out[0]["best_price"], 15000)
        self.assertIsInstance(out[0]["score"], float)

    def test_top_limits_count(self):
        for i in range(1, 5):
            self._write_article(f"2026-06-0{i}-B00000000{i}.json", _article(f"B00000000{i}"))
        self.assertEqual(len(sel.select_targets(top=2)), 2)

    def test_existing_store_copy_excluded(self):
        self._write_article("2026-06-01-B000000001.json", _article("B000000001", best_price=15000))
        self._write_article("2026-06-01-B000000002.json", _article("B000000002", best_price=800))
        self._write_store("B000000001", {"x": "既存コピー"})
        out = sel.select_targets(top=5)
        self.assertEqual([t["asin"] for t in out], ["B000000002"])

    def test_include_existing_keeps_store_copies(self):
        self._write_article("2026-06-01-B000000001.json", _article("B000000001"))
        self._write_store("B000000001", {"x": "既存コピー"})
        out = sel.select_targets(top=5, include_existing=True)
        self.assertEqual([t["asin"] for t in out], ["B000000001"])

    def test_published_excluded(self):
        self._write_article("2026-06-01-B000000001.json", _article("B000000001"))
        self._write_article("2026-06-02-B000000002.json", _article("B000000002"))
        pst.STATE_FILE.write_text('{"published": ["B000000001"], "updated": null}', encoding="utf-8")
        out = sel.select_targets(top=5)
        self.assertEqual([t["asin"] for t in out], ["B000000002"])


if __name__ == "__main__":
    unittest.main()
