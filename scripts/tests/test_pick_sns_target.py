"""pick_sns_target.py の #1580 スコアリング選定ロジックのテスト。

実行: amazon-clone 直下から `python -m unittest scripts.tests.test_pick_sns_target`
"""
import json
import math
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pick_sns_target as pst  # noqa: E402


def _article(asin, brand="レゴ", edu_domains=None, best_price=3000, date="2026-06-01"):
    """score_calculator が education 軸を算出できる最小記事 dict。"""
    return {
        "slug": f"{date}-{asin}",
        "date": f"{date}T10:00:00+09:00",
        "product": {
            "asin": asin,
            "name": f"商品 {asin}",
            "brand": brand,
            "edu_domains": edu_domains if edu_domains is not None else ["STEM", "想像"],
            "best_price": best_price,
        },
    }


class NormalizationTest(unittest.TestCase):
    def test_edu_norm_floor_and_ceil(self):
        self.assertEqual(pst._edu_norm(2.0), 0.0)
        self.assertEqual(pst._edu_norm(5.0), 1.0)
        self.assertAlmostEqual(pst._edu_norm(3.5), 0.5)

    def test_edu_norm_clamps_out_of_range(self):
        self.assertEqual(pst._edu_norm(1.0), 0.0)   # 軸下限割れ → 0
        self.assertEqual(pst._edu_norm(9.9), 1.0)   # 軸上限超え → 1
        self.assertEqual(pst._edu_norm(None), 0.0)  # 不明 → 0 (rankable)

    def test_price_norm_monotonic_and_saturating(self):
        self.assertEqual(pst._price_norm(None), 0.0)
        self.assertEqual(pst._price_norm(0), 0.0)
        self.assertEqual(pst._price_norm(pst.PRICE_MIN), 0.0)
        self.assertEqual(pst._price_norm(pst.PRICE_MAX), 1.0)
        self.assertEqual(pst._price_norm(99999), 1.0)  # 上端 saturate
        # 高いほど高得点 (高単価=アフィ報酬大)
        self.assertLess(pst._price_norm(2000), pst._price_norm(8000))

    def test_date_int(self):
        self.assertEqual(pst._date_int("2026-06-04-B0XXXXXXXX.json"), 20260604)
        self.assertEqual(pst._date_int("garbage.json"), 0)


class RankingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig_dir = pst.ARTICLES_DIR
        pst.ARTICLES_DIR = self.dir

    def tearDown(self):
        pst.ARTICLES_DIR = self._orig_dir
        self._tmp.cleanup()

    def _write(self, name, article):
        (self.dir / name).write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")

    def test_higher_edu_and_price_ranks_first(self):
        # 高知育・高価格
        self._write("2026-06-01-B000000001.json",
                    _article("B000000001", edu_domains=["STEM", "言語", "運動", "想像"], best_price=15000))
        # 低知育・低価格
        self._write("2026-06-01-B000000002.json",
                    _article("B000000002", edu_domains=[], best_price=800))
        ranked = pst.rank_candidates(set())
        self.assertEqual(ranked[0].asin, "B000000001")
        self.assertGreater(ranked[0].score, ranked[-1].score)

    def test_sidecar_files_excluded_and_do_not_shadow(self):
        # 本体記事 + 同 ASIN の quality サイドカー (product 無し)
        self._write("2026-06-01-B000000003.json",
                    _article("B000000003", best_price=12000))
        (self.dir / "2026-06-01-B000000003.quality.json").write_text(
            json.dumps({"slug": "x", "total_score": 88, "passed": True}), encoding="utf-8")
        ranked = pst.rank_candidates(set())
        # 候補は本体 1 件のみ。サイドカーが shadow して score 0 にしていない。
        self.assertEqual([c.asin for c in ranked], ["B000000003"])
        self.assertGreater(ranked[0].score, 0.0)

    def test_published_filtered_out(self):
        self._write("2026-06-01-B000000004.json", _article("B000000004"))
        self._write("2026-06-02-B000000005.json", _article("B000000005"))
        ranked = pst.rank_candidates({"B000000004"})
        self.assertEqual([c.asin for c in ranked], ["B000000005"])

    def test_no_hard_cutoff_low_score_still_returned(self):
        # 全件が低知育・低価格でも None にならず最上位を返す (枯渇回避)
        self._write("2026-06-01-B000000006.json",
                    _article("B000000006", edu_domains=[], best_price=300))
        self.assertIsNotNone(pst.pick_next(set()))

    def test_tiebreak_prefers_newer_date(self):
        # 同一スコア (同 brand/domains/price) は date 降順で新しい記事を優先
        self._write("2026-05-01-B000000007.json", _article("B000000007", date="2026-05-01"))
        self._write("2026-06-01-B000000008.json", _article("B000000008", date="2026-06-01"))
        ranked = pst.rank_candidates(set())
        self.assertAlmostEqual(ranked[0].score, ranked[1].score)
        self.assertEqual(ranked[0].asin, "B000000008")


if __name__ == "__main__":
    unittest.main()
