"""build_post.py の #508 関連ロジック (競合カード IVS スコア + 点数近接競合) の純粋関数テスト。

実行: amazon-clone 直下から `python -m unittest scripts.tests.test_competitor_internal_score`
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import build_post as bp  # noqa: E402


def _make_article(asin, score_100, ivs=4.5, price=2000, name=None):
    return {
        "slug": f"2026-05-30-{asin}",
        "product": {
            "asin": asin,
            "name": name or f"商品 {asin}",
            "image": f"https://example.com/{asin}.jpg",
            "ivs_score": ivs,
            "ivs_detail": {"total_100": score_100},
            "prices": {"amazon": {"price": price}},
        },
    }


class BuildArticleIndexTests(unittest.TestCase):
    def test_extracts_score_image_price(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "a.json").write_text(json.dumps(_make_article("B01AAAAAAA", 85)), encoding="utf-8")
            (root / "b.json").write_text(json.dumps(_make_article("B02BBBBBBB", 72, ivs=3.6, price=4500)), encoding="utf-8")
            idx = bp._build_article_index(root)
        self.assertEqual(set(idx), {"B01AAAAAAA", "B02BBBBBBB"})
        self.assertEqual(idx["B01AAAAAAA"]["ivs_score_100"], 85)
        self.assertEqual(idx["B01AAAAAAA"]["amazon_price"], 2000)
        self.assertEqual(idx["B02BBBBBBB"]["ivs_score"], 3.6)
        self.assertEqual(idx["B02BBBBBBB"]["amazon_price"], 4500)

    def test_skips_quality_sidecars(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "a.json").write_text(json.dumps(_make_article("B01CCCCCCC", 80)), encoding="utf-8")
            (root / "a.quality.json").write_text("{}", encoding="utf-8")
            (root / "a.seo.json").write_text("{}", encoding="utf-8")
            (root / "a.enrichment.json").write_text("{}", encoding="utf-8")
            idx = bp._build_article_index(root)
        self.assertEqual(list(idx), ["B01CCCCCCC"])

    def test_legacy_wrapper_returns_slug_map(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "a.json").write_text(json.dumps(_make_article("B01DDDDDDD", 60)), encoding="utf-8")
            m = bp._build_asin_to_slug_map(root)
        self.assertEqual(m, {"B01DDDDDDD": "2026-05-30-B01DDDDDDD"})


class AttachInternalLinksTests(unittest.TestCase):
    def setUp(self):
        self.index = {
            "B01XXXXXXX": {"slug": "2026-05-29-B01XXXXXXX", "ivs_score_100": 88, "ivs_score": 4.4,
                           "name": "競合A", "image": "", "amazon_price": 2500},
            "B02YYYYYYY": {"slug": "2026-05-28-B02YYYYYYY", "ivs_score_100": 0, "ivs_score": 0,
                           "name": "競合B", "image": "", "amazon_price": 1800},
        }

    def _data(self, asin="B00SELFSELF", competitors=None):
        return {
            "product": {"asin": asin, "ivs_detail": {"total_100": 80},
                        "prices": {"amazon": {"price": 2000}}},
            "competitive_analysis": competitors or [],
        }

    def test_attaches_score_and_url(self):
        data = self._data(competitors=[{"asin": "B01XXXXXXX", "name": "x"}])
        bp._attach_internal_links(data, self.index, "/amazon")
        c = data["competitive_analysis"][0]
        self.assertEqual(c["internal_url"], "/amazon/products/b01xxxxxxx/")
        self.assertEqual(c["internal_ivs_score_100"], 88)
        self.assertAlmostEqual(c["internal_ivs_score"], 4.4)

    def test_skips_self_asin(self):
        data = self._data(asin="B01XXXXXXX",
                          competitors=[{"asin": "B01XXXXXXX", "name": "self"}])
        bp._attach_internal_links(data, self.index, "")
        self.assertNotIn("internal_url", data["competitive_analysis"][0])

    def test_skips_zero_score(self):
        """ivs_score_100=0 でも internal_url は付くが score badge は無し"""
        data = self._data(competitors=[{"asin": "B02YYYYYYY", "name": "y"}])
        bp._attach_internal_links(data, self.index, "")
        c = data["competitive_analysis"][0]
        self.assertIn("internal_url", c)
        self.assertNotIn("internal_ivs_score_100", c)

    def test_legacy_slug_map_shape_back_compat(self):
        """旧 {asin: slug} 形式でも internal_url だけは付く"""
        legacy = {"B01XXXXXXX": "2026-05-29-B01XXXXXXX"}
        data = self._data(competitors=[{"asin": "B01XXXXXXX", "name": "x"}])
        bp._attach_internal_links(data, legacy, "")
        c = data["competitive_analysis"][0]
        self.assertEqual(c["internal_url"], "/products/b01xxxxxxx/")
        self.assertNotIn("internal_ivs_score_100", c)


class RecommendNearbyCompetitorsTests(unittest.TestCase):
    def _data(self, self_asin="B00SELFSELF", target_score=80, target_price=3000, ca=None):
        return {
            "product": {
                "asin": self_asin,
                "ivs_detail": {"total_100": target_score},
                "prices": {"amazon": {"price": target_price}},
            },
            "competitive_analysis": ca if ca is not None else [],
        }

    def _index(self, *entries):
        return {
            a: {"slug": f"slug-{a}", "ivs_score_100": s, "ivs_score": s / 20.0,
                "name": f"name-{a}", "image": f"img-{a}", "amazon_price": p}
            for a, s, p in entries
        }

    def test_picks_within_window_and_sorts_by_proximity(self):
        idx = self._index(
            ("B01NEAR0001", 78, 2800),  # delta 2
            ("B02NEAR0002", 84, 3200),  # delta 4
            ("B03FAR00003", 70, 2100),  # delta 10 — out of window
            ("B04NEAR0004", 80, 3000),  # delta 0
        )
        data = self._data(target_score=80)
        bp._recommend_nearby_competitors(data, idx, "/amazon", window=5, limit=3)
        rec = [c["asin"] for c in data["competitive_analysis"]]
        self.assertEqual(rec, ["B04NEAR0004", "B01NEAR0001", "B02NEAR0002"])
        for c in data["competitive_analysis"]:
            self.assertTrue(c["recommended_by_score"])
            self.assertTrue(c["internal_url"].startswith("/amazon/products/"))

    def test_excludes_self_and_existing_competitors(self):
        idx = self._index(
            ("B00SELFSELF", 80, 3000),  # self → excluded
            ("B01EXIST001", 82, 3100),  # already in CA → excluded
            ("B02NEW00002", 78, 2900),  # eligible
        )
        data = self._data(target_score=80, ca=[
            {"asin": "B01EXIST001", "name": "existing"},
        ])
        bp._recommend_nearby_competitors(data, idx, "", limit=5)
        new_asins = [c["asin"] for c in data["competitive_analysis"] if c.get("recommended_by_score")]
        self.assertEqual(new_asins, ["B02NEW00002"])

    def test_no_target_score_is_noop(self):
        idx = self._index(("B01N0SCORE0", 80, 3000))
        data = {"product": {"asin": "X", "ivs_detail": {}}, "competitive_analysis": []}
        bp._recommend_nearby_competitors(data, idx, "")
        self.assertEqual(data["competitive_analysis"], [])

    def test_zero_score_index_entries_skipped(self):
        idx = self._index(("B01ZERO0001", 0, 2000), ("B02HAS00002", 79, 2800))
        data = self._data(target_score=80)
        bp._recommend_nearby_competitors(data, idx, "", limit=3)
        self.assertEqual([c["asin"] for c in data["competitive_analysis"]], ["B02HAS00002"])

    def test_limit_caps_recommendations(self):
        idx = self._index(*[(f"B0{i:02d}NEAR000", 80, 2500) for i in range(1, 6)])
        data = self._data(target_score=80)
        bp._recommend_nearby_competitors(data, idx, "", limit=2)
        self.assertEqual(len(data["competitive_analysis"]), 2)


class SamePriceBandTests(unittest.TestCase):
    """#586: 同価格帯リンク — PRICE_BANDS と整合し /cospa/#anchor で抜け出せる。"""

    def _index(self, *entries):
        idx = {}
        for asin, score_100, price in entries:
            idx[asin] = {"slug": f"2026-05-30-{asin}", "ivs_score_100": score_100,
                         "ivs_score": float(score_100) / 20, "name": f"商品 {asin}",
                         "image": f"https://example.com/{asin}.jpg", "amazon_price": price}
        return idx

    def _data(self, *, target_price=3500, target_asin="B00SELFSELF", ca=None):
        return {
            "product": {"asin": target_asin, "ivs_detail": {"total_100": 80},
                        "prices": {"amazon": {"price": target_price}}},
            "competitive_analysis": ca or [],
        }

    def test_picks_same_band_orders_by_ivs_desc(self):
        # target ¥3500 → "3000-5000" 帯
        idx = self._index(
            ("B01SAME0001", 70, 3200),  # 同帯
            ("B02SAME0002", 88, 4800),  # 同帯 (最高スコア)
            ("B03OTHER003", 90, 2500),  # 帯外 (2000-3000)
            ("B04SAME0004", 75, 3700),  # 同帯
        )
        data = self._data(target_price=3500)
        bp._recommend_same_price_band(data, idx, "", limit=4)
        spb = data["same_price_band"]
        self.assertEqual(spb["band_key"], "3000-5000")
        self.assertEqual([s["asin"] for s in spb["items"]],
                         ["B02SAME0002", "B04SAME0004", "B01SAME0001"])
        self.assertEqual(spb["items"][0]["internal_ivs_score_100"], 88)

    def test_band_url_uses_site_base_path(self):
        idx = self._index(("B01SAMEX001", 80, 3500))
        data = self._data(target_price=3500)
        bp._recommend_same_price_band(data, idx, "/amazon", limit=2)
        self.assertEqual(data["same_price_band"]["band_url"],
                         "/amazon/cospa/#cospa-band-pane-3000-5000")
        self.assertEqual(data["same_price_band"]["items"][0]["internal_url"],
                         "/amazon/products/b01samex001/")

    def test_excludes_self_and_existing_competitors(self):
        idx = self._index(
            ("B00SELFSELF", 80, 3500),   # self
            ("B01ALREADYY1", 78, 3300),  # already in CA
            ("B02FRESH0002", 70, 3800),  # eligible
        )
        data = self._data(target_price=3500, target_asin="B00SELFSELF",
                          ca=[{"asin": "B01ALREADYY1", "name": "x"}])
        bp._recommend_same_price_band(data, idx, "", limit=4)
        self.assertEqual([s["asin"] for s in data["same_price_band"]["items"]],
                         ["B02FRESH0002"])

    def test_skips_when_price_outside_all_bands(self):
        idx = self._index(("B01ANYTHIN1", 80, 3500))
        data = self._data(target_price=99999)  # 帯外
        bp._recommend_same_price_band(data, idx, "")
        self.assertNotIn("same_price_band", data)

    def test_skips_when_no_amazon_price(self):
        idx = self._index(("B01ANYTHIN2", 80, 3500))
        data = {"product": {"asin": "X", "ivs_detail": {"total_100": 80}, "prices": {"amazon": {}}},
                "competitive_analysis": []}
        bp._recommend_same_price_band(data, idx, "")
        self.assertNotIn("same_price_band", data)

    def test_skips_when_no_peers_in_band(self):
        idx = self._index(("B01OTHERX01", 80, 1500))  # 別帯のみ
        data = self._data(target_price=3500)
        bp._recommend_same_price_band(data, idx, "")
        self.assertNotIn("same_price_band", data)

    def test_skips_zero_score_peers(self):
        idx = self._index(("B01ZEROSCO1", 0, 3500), ("B02OK000002", 65, 3600))
        data = self._data(target_price=3500)
        bp._recommend_same_price_band(data, idx, "", limit=3)
        self.assertEqual([s["asin"] for s in data["same_price_band"]["items"]],
                         ["B02OK000002"])

    def test_limit_caps_output(self):
        idx = self._index(*[(f"B0{i:02d}MANY000", 90 - i, 3500) for i in range(1, 9)])
        data = self._data(target_price=3500)
        bp._recommend_same_price_band(data, idx, "", limit=3)
        self.assertEqual(len(data["same_price_band"]["items"]), 3)


class ResolvePriceBandTests(unittest.TestCase):
    def test_resolves_boundaries(self):
        self.assertEqual(bp._resolve_price_band(1000)["key"], "1000-2000")
        self.assertEqual(bp._resolve_price_band(1999)["key"], "1000-2000")
        self.assertEqual(bp._resolve_price_band(3000)["key"], "3000-5000")
        self.assertEqual(bp._resolve_price_band(999)["key"], "lt1000")

    def test_returns_none_for_out_of_range(self):
        self.assertIsNone(bp._resolve_price_band(0))
        self.assertIsNone(bp._resolve_price_band(-100))
        self.assertIsNone(bp._resolve_price_band(50000))


if __name__ == "__main__":
    unittest.main()
