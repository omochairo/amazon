"""Unit tests for build_brand_hub_stats.py (#731 Prep).

Coverage:
1. parse_age_min_months - 歳 / 歳半 / ヶ月 / 不正値
2. _series_candidates  - tags[0] 除外 / 汎用 stopword 除去 / 空入力
3. aggregate           - 集計 / 記事数<3 のしきい値 / 空入力 / 不正 JSON skip /
                         representative = 最高 IVS / lowest_price / exclude ノーブランド
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

import build_brand_hub_stats as bhs  # noqa: E402


def _article(asin: str, brand: str, ivs_100: int, price: int, age: str,
             tags: list[str], image: str = "https://x/i.jpg") -> dict:
    return {
        "slug": f"2026-05-30-{asin}",
        "tags": tags,
        "persona_fit": {"age_range": age},
        "product": {
            "asin": asin,
            "brand": brand,
            "image": image,
            "ivs_detail": {"total_100": ivs_100},
            "best_price": price,
        },
    }


def _write_articles(root: pathlib.Path, items: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for a in items:
        (root / f"{a['slug']}.json").write_text(
            json.dumps(a, ensure_ascii=False), encoding="utf-8"
        )


class TestAgeParser(unittest.TestCase):
    def test_years_with_ijou(self):
        self.assertEqual(bhs.parse_age_min_months("3歳以上"), 36)

    def test_years_with_tilde(self):
        self.assertEqual(bhs.parse_age_min_months("5歳〜"), 60)

    def test_years_with_han(self):
        self.assertEqual(bhs.parse_age_min_months("1歳半〜"), 18)

    def test_months(self):
        self.assertEqual(bhs.parse_age_min_months("10ヶ月〜"), 10)

    def test_months_kana_variants(self):
        self.assertEqual(bhs.parse_age_min_months("6か月〜"), 6)
        self.assertEqual(bhs.parse_age_min_months("6カ月〜"), 6)

    def test_zero(self):
        self.assertEqual(bhs.parse_age_min_months("0歳〜"), 0)

    def test_unparseable(self):
        self.assertIsNone(bhs.parse_age_min_months("all ages"))
        self.assertIsNone(bhs.parse_age_min_months(""))
        self.assertIsNone(bhs.parse_age_min_months(None))


class TestSeriesCandidates(unittest.TestCase):
    def test_skips_first_tag_as_brand(self):
        out = bhs._series_candidates(["レゴ", "デュプロ", "クラシック"])
        self.assertEqual(out, ["デュプロ", "クラシック"])

    def test_filters_generic_stopwords(self):
        out = bhs._series_candidates(
            ["タカラトミー", "プラレール", "知育玩具", "プレゼント", "ブロック"]
        )
        self.assertEqual(out, ["プラレール"])

    def test_filters_life_event_tags(self):
        out = bhs._series_candidates(
            ["Mamimami Home", "プレイマット", "出産祝い", "誕生日", "クリスマス"]
        )
        self.assertEqual(out, ["プレイマット"])

    def test_filters_age_tags(self):
        out = bhs._series_candidates(
            ["Mamimami Home", "プレイマット", "0歳", "1歳半", "6ヶ月", "12カ月"]
        )
        self.assertEqual(out, ["プレイマット"])

    def test_filters_demographic_tags(self):
        out = bhs._series_candidates(
            ["ブランド", "シリーズA", "男の子", "女の子", "赤ちゃん", "幼児"]
        )
        self.assertEqual(out, ["シリーズA"])

    def test_empty(self):
        self.assertEqual(bhs._series_candidates([]), [])
        self.assertEqual(bhs._series_candidates(None), [])

    def test_single_tag_no_series(self):
        # tags[0] only -> nothing left after brand exclusion
        self.assertEqual(bhs._series_candidates(["レゴ"]), [])


class TestAggregate(unittest.TestCase):
    def test_threshold_under_three_returns_count_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A2", "レゴ(LEGO)", 85, 2000, "5歳〜", ["レゴ", "クラシック"]),
            ])
            payload = bhs.aggregate(root)
        self.assertEqual(payload["brand_count_total"], 1)
        self.assertEqual(payload["brand_count_narrative"], 0)
        rec = payload["brands"]["レゴ"]
        self.assertEqual(rec, {"count": 2})

    def test_full_aggregate_three_articles(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1500, "3歳以上",
                         ["レゴ", "デュプロ", "知育玩具"], image="https://x/1.jpg"),
                _article("A2", "レゴ(LEGO)", 90, 3000, "5歳〜",
                         ["レゴ", "クラシック"], image="https://x/2.jpg"),
                _article("A3", "レゴ(LEGO)", 85, 999, "1歳半〜",
                         ["レゴ", "デュプロ"], image="https://x/3.jpg"),
            ])
            payload = bhs.aggregate(root)

        self.assertEqual(payload["brand_count_total"], 1)
        self.assertEqual(payload["brand_count_narrative"], 1)
        rec = payload["brands"]["レゴ"]
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["tier"], "S")
        self.assertEqual(rec["avg_ivs_100"], 85.0)
        # ages: 36, 60, 18 -> avg 38.0
        self.assertEqual(rec["avg_age_min_months"], 38.0)
        # representative = highest ivs (A2, 90)
        self.assertEqual(rec["representative_asin"], "A2")
        self.assertEqual(rec["representative_image"], "https://x/2.jpg")
        # lowest_price across the three
        self.assertEqual(rec["lowest_price"], 999)
        # series: デュプロ x2, クラシック x1 -> top3 in count order
        self.assertEqual(rec["top_3_series"][0], "デュプロ")
        self.assertIn("クラシック", rec["top_3_series"])

    def test_excludes_no_brand(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("N1", "謎ブランドXYZ", 80, 1000, "3歳以上", ["X"]),
                _article("N2", "謎ブランドXYZ", 80, 1000, "3歳以上", ["X"]),
                _article("N3", "謎ブランドXYZ", 80, 1000, "3歳以上", ["X"]),
            ])
            payload = bhs.aggregate(root)
        # exclude_from_taxonomy=True for ノーブランド -> brand not in output
        self.assertEqual(payload["brand_count_total"], 0)

    def test_noindex_brand_full_aggregate(self):
        # Jasonwell は brand_taxonomy.yaml で noindex: true (epic #2126 P4)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("J1", "Jasonwell", 80, 1000, "3歳以上", ["X"]),
                _article("J2", "Jasonwell", 80, 1000, "3歳以上", ["X"]),
                _article("J3", "Jasonwell", 80, 1000, "3歳以上", ["X"]),
            ])
            payload = bhs.aggregate(root)
        rec = payload["brands"]["Jasonwell"]
        self.assertTrue(rec["noindex"])

    def test_noindex_brand_under_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("J1", "Jasonwell", 80, 1000, "3歳以上", ["X"]),
                _article("J2", "Jasonwell", 80, 1000, "3歳以上", ["X"]),
            ])
            payload = bhs.aggregate(root)
        self.assertEqual(payload["brands"]["Jasonwell"], {"count": 2, "noindex": True})

    def test_non_noindex_brand_omits_flag(self):
        # noindex でないブランドは noindex キーを出力しない (JSON を軽く保つ)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ"]),
                _article("A2", "レゴ(LEGO)", 85, 2000, "5歳〜", ["レゴ"]),
                _article("A3", "レゴ(LEGO)", 85, 999, "1歳半〜", ["レゴ"]),
            ])
            payload = bhs.aggregate(root)
        self.assertNotIn("noindex", payload["brands"]["レゴ"])

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            root.mkdir()
            payload = bhs.aggregate(root)
        self.assertEqual(payload["brand_count_total"], 0)
        self.assertEqual(payload["brands"], {})

    def test_skips_malformed_and_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            root.mkdir(parents=True)
            # valid trio
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A2", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A3", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
            ])
            # sidecars MUST be ignored
            (root / "2026-05-30-A1.quality.json").write_text("{}", encoding="utf-8")
            (root / "2026-05-30-A1.enrichment.json").write_text("{}", encoding="utf-8")
            (root / "2026-05-30-A1.seo.json").write_text("{}", encoding="utf-8")
            # malformed JSON
            (root / "broken.json").write_text("not json", encoding="utf-8")

            payload = bhs.aggregate(root)
        self.assertEqual(payload["brand_count_total"], 1)
        self.assertEqual(payload["brands"]["レゴ"]["count"], 3)

    def test_write_output_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            out = pathlib.Path(td) / "out" / "brand_hub.json"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A2", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A3", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
            ])
            payload = bhs.aggregate(root)
            bhs.write_output(payload, out)
            self.assertTrue(out.exists())
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(loaded["brands"]["レゴ"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
