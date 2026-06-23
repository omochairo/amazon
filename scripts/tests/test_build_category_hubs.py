"""Unit tests for build_category_hubs.py (Issue #2687 柱1).

Coverage:
1. _matches      - include hit / exclude veto / no-match.
2. build_hub     - min_ivs floor, ivs_100 desc sort, price tiebreak, top_n cap,
                   text-index gating (no theme text -> excluded).
3. serialize_hub - shape (generated_at/type/count/items) + rank numbering +
                   lowercased url_internal via _record_to_payload_common.
4. run           - end-to-end on a tmp fixture writes hugo/data/features/<theme>.json.
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

import build_category_hubs as bch  # noqa: E402
from build_feature_lists import ArticleRecord  # noqa: E402


def _rec(asin, ivs_score, ivs_100, best_price, slug=None):
    return ArticleRecord(
        asin=asin,
        slug=slug or asin,
        name=f"name-{asin}",
        image=None,
        ivs_score=ivs_score,
        ivs_100=ivs_100,
        best_price=best_price,
        best_platform="amazon",
        amazon_url=f"https://amazon/{asin}",
    )


class MatchesTest(unittest.TestCase):
    def test_include_hit(self):
        self.assertTrue(bch._matches("これは英語のおもちゃ", bch.THEMES["english"]))

    def test_exclude_veto(self):
        theme = {"include": ["英語"], "exclude": ["建設"]}
        self.assertFalse(bch._matches("英語 英文建設キット", theme))

    def test_no_match(self):
        self.assertFalse(bch._matches("ただの積み木", bch.THEMES["english"]))


class BuildHubTest(unittest.TestCase):
    def setUp(self):
        self.theme = {"include": ["英語"], "exclude": []}
        self.records = [
            _rec("A", 4.5, 90, 2000),
            _rec("B", 4.8, 95, 3000),
            _rec("C", 3.0, 60, 1000),   # below min_ivs
            _rec("D", 4.0, 95, 1500),   # ties B on ivs_100, cheaper
            _rec("E", 4.2, 80, 500),    # has theme text but we omit -> excluded
        ]
        self.text = {
            "A": "英語 abc", "B": "英語 フォニックス",
            "C": "英語 かるた", "D": "英語 タブレット",
            # E intentionally absent from index
        }

    def test_min_ivs_and_text_gate(self):
        out = bch.build_hub(self.records, self.text, self.theme,
                            top_n=10, min_ivs=3.8)
        asins = [r.asin for r in out]
        self.assertNotIn("C", asins)  # below min_ivs
        self.assertNotIn("E", asins)  # no theme text

    def test_sort_and_price_tiebreak(self):
        out = bch.build_hub(self.records, self.text, self.theme,
                            top_n=10, min_ivs=3.8)
        # ivs_100 desc; B(95,3000) vs D(95,1500) tie -> cheaper D first
        self.assertEqual([r.asin for r in out], ["D", "B", "A"])

    def test_top_n_cap(self):
        out = bch.build_hub(self.records, self.text, self.theme,
                            top_n=1, min_ivs=3.8)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].asin, "D")


class SerializeTest(unittest.TestCase):
    def test_shape_and_rank(self):
        items = [_rec("AbC", 4.5, 90, 2000, slug="Slug-One")]
        payload = bch.serialize_hub(items, "english", "2026-06-23T00:00:00Z")
        self.assertEqual(payload["type"], "english")
        self.assertEqual(payload["count"], 1)
        it = payload["items"][0]
        self.assertEqual(it["rank"], 1)
        self.assertEqual(it["url_internal"], "/posts/slug-one/")  # lowercased


class RunTest(unittest.TestCase):
    def test_end_to_end_writes_theme_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()
            doc = {
                "slug": "eigo-toy",
                "title": "英語のおもちゃ",
                "tags": ["英語"],
                "keywords": ["フォニックス"],
                "product": {
                    "asin": "B0ENG1",
                    "name": "英語トイ",
                    "brand": "ACME",
                    "ivs_score": 4.5,
                    "best_price": 2000,
                    "best_platform": "amazon",
                    "prices": {"amazon": {"url": "https://amazon/B0ENG1"}},
                },
            }
            (adir / "2026-06-01-B0ENG1.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            out = pathlib.Path(tmp) / "features"
            # min_ivs=0: load_articles recomputes the score via score_calculator,
            # and the minimal fixture scores low; this test exercises pipeline
            # wiring (load -> classify -> serialize -> write), not the floor.
            counts = bch.run(adir, out, top_n=24, min_ivs=0.0,
                             themes=["english"])
            self.assertEqual(counts["english"], 1)
            data = json.loads((out / "english.json").read_text(encoding="utf-8"))
            self.assertEqual(data["items"][0]["asin"], "B0ENG1")


class ParseMinMonthsTest(unittest.TestCase):
    def test_years_variants(self):
        self.assertEqual(bch.parse_min_months("3歳以上"), 36)
        self.assertEqual(bch.parse_min_months("3歳〜"), 36)
        self.assertEqual(bch.parse_min_months("3歳〜6歳"), 36)  # 最小を採る
        self.assertEqual(bch.parse_min_months("3才"), 36)       # 才→歳

    def test_half_year_variants(self):
        self.assertEqual(bch.parse_min_months("1歳6ヶ月〜"), 18)
        self.assertEqual(bch.parse_min_months("1.5歳〜"), 18)
        self.assertEqual(bch.parse_min_months("1歳半〜"), 18)

    def test_months_only(self):
        self.assertEqual(bch.parse_min_months("0ヶ月〜"), 0)
        self.assertEqual(bch.parse_min_months("6ヶ月〜"), 6)
        self.assertEqual(bch.parse_min_months("18ヶ月〜"), 18)

    def test_none_cases(self):
        self.assertIsNone(bch.parse_min_months(None))
        self.assertIsNone(bch.parse_min_months(""))
        self.assertIsNone(bch.parse_min_months("対象年齢の記載なし"))


class BuildAgeHubTest(unittest.TestCase):
    def setUp(self):
        # 1歳 hub = [12, 24)
        self.hub = bch.AGE_HUBS["age-1"]
        self.records = [
            _rec("A", 4.5, 90, 2000),
            _rec("B", 4.8, 95, 3000),
            _rec("C", 3.0, 60, 1000),   # below min_ivs
            _rec("D", 4.0, 95, 1500),   # ties B, cheaper
            _rec("E", 4.2, 80, 500),    # min_months out of range
            _rec("F", 4.2, 80, 700),    # no age data
        ]
        self.mm = {"A": 12, "B": 18, "C": 12, "D": 23, "E": 36, "F": None}

    def test_range_and_floor_gate(self):
        out = bch.build_age_hub(self.records, self.mm, self.hub,
                                top_n=10, min_ivs=3.8)
        asins = [r.asin for r in out]
        self.assertNotIn("C", asins)  # below min_ivs
        self.assertNotIn("E", asins)  # 36mo -> not in [12,24)
        self.assertNotIn("F", asins)  # None age

    def test_sort_price_tiebreak(self):
        out = bch.build_age_hub(self.records, self.mm, self.hub,
                                top_n=10, min_ivs=3.8)
        self.assertEqual([r.asin for r in out], ["D", "B", "A"])


class RunAgeHubTest(unittest.TestCase):
    def test_end_to_end_writes_age_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()
            doc = {
                "slug": "ichi-toy",
                "title": "1歳のおもちゃ",
                "persona_fit": {"age_range": "1歳〜"},
                "product": {
                    "asin": "B0AGE1",
                    "name": "つみき",
                    "ivs_score": 4.5,
                    "best_price": 2000,
                    "best_platform": "amazon",
                    "prices": {"amazon": {"url": "https://amazon/B0AGE1"}},
                },
            }
            (adir / "2026-06-01-B0AGE1.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            out = pathlib.Path(tmp) / "features"
            counts = bch.run(adir, out, top_n=24, min_ivs=0.0,
                             themes=[], age_hubs=["age-1"])
            self.assertEqual(counts["age-1"], 1)
            data = json.loads((out / "age-1.json").read_text(encoding="utf-8"))
            self.assertEqual(data["type"], "age-1")
            self.assertEqual(data["items"][0]["asin"], "B0AGE1")


if __name__ == "__main__":
    unittest.main()
