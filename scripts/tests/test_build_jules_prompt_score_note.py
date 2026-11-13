"""build_jules_prompt._score_note のテスト (navi-brain#165 T1)。

jules/PROMPT_TEMPLATE.md (private repo) は読まないので、手元に jules/ が無くても回る。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import build_jules_prompt as bjp  # noqa: E402
import score_claims  # noqa: E402

_ITEM = {"asin": "B0TESTNOTE", "title": "ノーブランド品 木製パズル 知育", "price": 1800}


class ScoreNoteTest(unittest.TestCase):
    def test_new_asin_gives_projection_and_bans(self):
        with mock.patch.object(bjp, "_prior_article", return_value=None), \
                mock.patch.object(bjp, "_amazon_item", return_value=_ITEM):
            note = bjp._score_note("B0TESTNOTE")
        self.assertIn("サイト表示の点数", note)
        self.assertIn("見込み", note)
        # 「ノーブランド品」はブランド名として扱わない
        self.assertIn("辞書に無い (tier D) 前提", note)
        # tier D は認証があっても安全性 2.6 止まり → 言い切り禁止が確定で出る
        self.assertIn("安全性の言い切りは書かない", note)
        self.assertIn("自信を持って推せる", note)

    def test_rewrite_uses_prior_article(self):
        prior = {"slug": "2026-01-01-B0TESTNOTE",
                 "product": {"asin": "B0TESTNOTE", "brand": "Hueyaus", "certifications": [],
                             "best_price": 1800}}
        with mock.patch.object(bjp, "_prior_article", return_value=prior):
            note = bjp._score_note("B0TESTNOTE")
        view = score_claims.compute_view(prior)
        self.assertIn(f"総合 {view.total_100} 点", note)
        self.assertIn("前の記事の内容のまま", note)

    def test_known_brand_from_title(self):
        item = dict(_ITEM, title="レゴ(LEGO) デュプロ はじめてのデュプロ")
        from brand_normalizer import normalize
        self.assertEqual(normalize(bjp._guess_brand(item["title"])).canonical, "レゴ")
        self.assertIsNone(bjp._guess_brand(_ITEM["title"]))

    def test_failure_returns_empty(self):
        with mock.patch.object(bjp, "_prior_article", side_effect=RuntimeError("x")):
            self.assertEqual(bjp._score_note("B0TESTNOTE"), "")
        with mock.patch.object(bjp, "_prior_article", return_value=None), \
                mock.patch.object(bjp, "_amazon_item", side_effect=SystemExit("no data")):
            self.assertEqual(bjp._score_note("B0TESTNOTE"), "")


if __name__ == "__main__":
    unittest.main()
