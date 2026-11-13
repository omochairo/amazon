"""関連のおすすめの共通条件 (年齢・種類) と 🌱 半年〜1年後 の選定 (navi-brain#165 T2)。

対象: build_post の _recommend_nearby_competitors / _recommend_same_price_band /
_recommend_future_age / _resolve_age_min_months / _build_article_index。
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


def _entry(asin, *, score=70, price=3500, age=36, types=("puzzle",), brand=None, series=()):
    return {
        "slug": f"2026-01-01-{asin}", "ivs_score_100": score, "ivs_score": score / 20,
        "name": f"商品 {asin}", "image": "", "amazon_price": price,
        "age_min_months": age, "toy_types": list(types), "brand": brand, "series": list(series),
    }


def _data(asin="B00SELF0000", *, age="3歳〜", tags=("パズル",), price=3500, brand=None):
    product = {"asin": asin, "name": "自分", "target_age": age, "prices": {"amazon": {"price": price}}}
    if brand:
        product["brand"] = brand
    return {"product": product, "tags": list(tags), "competitive_analysis": []}


class AgeCompatibleTests(unittest.TestCase):
    def test_tolerance(self):
        self.assertTrue(bp._rel_age_compatible(12, 24))
        self.assertFalse(bp._rel_age_compatible(12, 25))
        # 6 歳以上は 24 か月まで
        self.assertTrue(bp._rel_age_compatible(72, 96))
        self.assertFalse(bp._rel_age_compatible(72, 97))

    def test_unknown_and_adult(self):
        self.assertFalse(bp._rel_age_compatible(None, 36))
        self.assertFalse(bp._rel_age_compatible(36, None))
        self.assertFalse(bp._rel_age_compatible(204, 216))  # 大人向けは大人向け同士だけ
        self.assertTrue(bp._rel_age_compatible(216, 216))

    def test_type_stage(self):
        self.assertEqual(bp._rel_type_stage(["puzzle"], ["puzzle", "blocks"]), 1)
        self.assertIsNone(bp._rel_type_stage(["puzzle"], ["action"]))
        self.assertEqual(bp._rel_type_stage(["puzzle"], []), 2)
        self.assertEqual(bp._rel_type_stage([], ["action"]), 2)


class SeriesTagsTests(unittest.TestCase):
    def test_series_alias_kept_but_same_name_variants_dropped(self):
        # 「トミカ」はタカラトミーの傘下シリーズ。ブランド名そのものとシリーズでないタグは外す
        got = bp._series_tags("タカラトミー", ["トミカ", "タカラトミー", "タカラトミー(TAKARA TOMY)", "パズル"])
        self.assertIn("トミカ", got)
        self.assertNotIn("タカラトミー", got)
        self.assertNotIn("パズル", got)

    def test_name_variants_are_compared_folded(self):
        # name_variants は畳んだ表記の集合。BabyBus の「ベビーバス」は別表記でシリーズではない
        self.assertEqual(bp._series_tags("BabyBus", ["ベビーバス", "BabyBus", "babybus"]), [])
        self.assertEqual(bp._series_tags(None, ["トミカ"]), [])


class SamePriceBandFilterTests(unittest.TestCase):
    def test_drops_far_age_and_other_types_then_orders_by_score(self):
        idx = {
            "B01NERF0001": _entry("B01NERF0001", score=95, age=96, types=["action"]),   # 年齢・種類とも違う
            "B02OLD00002": _entry("B02OLD00002", score=90, age=72, types=["puzzle"]),   # 年齢差 36
            "B03BLOCK003": _entry("B03BLOCK003", score=88, age=36, types=["blocks"]),   # 種類が違う
            "B04UNK00004": _entry("B04UNK00004", score=85, age=30, types=[]),           # 種類不明 (2 段目)
            "B05PUZ00005": _entry("B05PUZ00005", score=60, age=24, types=["puzzle"]),   # 1 段目
            "B06PUZ00006": _entry("B06PUZ00006", score=50, age=40, types=["puzzle"]),   # 1 段目
            "B07NOAGE007": _entry("B07NOAGE007", score=99, age=None, types=["puzzle"]), # 年齢不明
        }
        data = _data(age="3歳〜")
        bp._recommend_same_price_band(data, idx, "", limit=4)
        # 1 段目 2 件 + 2 段目 1 件。並びは選んだ全体を点数の高い順
        self.assertEqual([c["asin"] for c in data["same_price_band"]["cards"]],
                         ["B04UNK00004", "B05PUZ00005", "B06PUZ00006"])

    def test_stage1_fills_before_stage2(self):
        idx = {f"B0{i}PUZ0000{i}": _entry(f"B0{i}PUZ0000{i}", score=50 + i) for i in range(1, 5)}
        idx["B09UNK00009"] = _entry("B09UNK00009", score=99, types=[])
        data = _data()
        bp._recommend_same_price_band(data, idx, "", limit=4)
        self.assertNotIn("B09UNK00009", [c["asin"] for c in data["same_price_band"]["cards"]])

    def test_fewer_than_two_hides_band(self):
        idx = {"B01PUZ00001": _entry("B01PUZ00001"),
               "B02NERF0002": _entry("B02NERF0002", types=["action"])}
        data = _data()
        bp._recommend_same_price_band(data, idx, "")
        self.assertNotIn("same_price_band", data)

    def test_self_age_unknown_hides_band(self):
        idx = {f"B0{i}PUZ0000{i}": _entry(f"B0{i}PUZ0000{i}") for i in range(1, 4)}
        data = _data(age="")
        bp._recommend_same_price_band(data, idx, "")
        self.assertNotIn("same_price_band", data)


class NearbyFilterTests(unittest.TestCase):
    def test_widens_to_10_only_when_5_is_short(self):
        idx = {
            "B01NEAR0001": _entry("B01NEAR0001", score=82),                      # ±2
            "B02NERF0002": _entry("B02NERF0002", score=80, types=["action"]),   # 種類が違う
            "B03WIDE0003": _entry("B03WIDE0003", score=89),                      # ±9
            "B04FAR00004": _entry("B04FAR00004", score=95),                      # ±15
        }
        data = _data()
        bp._recommend_nearby_competitors(data, idx, "", self_score=80)
        self.assertEqual([c["asin"] for c in data["competitive_analysis"]], ["B01NEAR0001", "B03WIDE0003"])
        self.assertEqual([c["score_delta"] for c in data["competitive_analysis"]], [2, 9])

    def test_nothing_within_10_adds_nothing(self):
        idx = {"B04FAR00004": _entry("B04FAR00004", score=95)}
        data = _data()
        data["competitive_analysis"] = [{"asin": "B0AMAZON001", "name": "Amazon の競合"}]
        bp._recommend_nearby_competitors(data, idx, "", self_score=80)
        # Amazon から取った競合カードはそのまま
        self.assertEqual([c["asin"] for c in data["competitive_analysis"]], ["B0AMAZON001"])

    def test_self_score_is_the_given_one_not_jules_cache(self):
        idx = {"B01AT60XXXX": _entry("B01AT60XXXX", score=60), "B02AT90XXXX": _entry("B02AT90XXXX", score=90)}
        data = _data()
        data["product"]["ivs_detail"] = {"total_100": 90}  # 古い Jules の値は見ない
        bp._recommend_nearby_competitors(data, idx, "", self_score=60)
        self.assertEqual([c["asin"] for c in data["competitive_analysis"]], ["B01AT60XXXX"])


class FutureAgeTests(unittest.TestCase):
    def test_gap_range_types_and_order(self):
        idx = {
            "B01SAME0001": _entry("B01SAME0001", age=12),                         # 年齢差 0
            "B02NEAR0002": _entry("B02NEAR0002", age=24, score=60),               # +12 (近い)
            "B03FAR00003": _entry("B03FAR00003", age=36, score=99),               # +24 (遠い)
            "B04TOOFAR04": _entry("B04TOOFAR04", age=48),                         # +36
            "B05NERF0005": _entry("B05NERF0005", age=24, types=["action"]),       # 種類が違う
            "B06BRAND006": _entry("B06BRAND006", age=36, score=10, brand="くもん出版"),  # 同じブランド
            "B07ADULT007": _entry("B07ADULT007", age=216),
        }
        data = _data(age="1歳〜", brand="くもん出版")
        bp._recommend_future_age(data, idx)
        cards = data["future_age_cards"]
        self.assertEqual([c["asin"] for c in cards], ["B06BRAND006", "B02NEAR0002", "B03FAR00003"])
        self.assertEqual(cards[1], {"path": "/posts/2026-01-01-B02NEAR0002", "asin": "B02NEAR0002",
                                    "age": 24, "gap": 12})

    def test_same_brand_capped_at_three(self):
        idx = {f"B0{i}BR00000{i}": _entry(f"B0{i}BR00000{i}", age=24, brand="レゴ", score=90 - i)
               for i in range(1, 6)}
        idx["B09OTHER009"] = _entry("B09OTHER009", age=24, score=10)
        data = _data(age="1歳〜", brand="レゴ")
        bp._recommend_future_age(data, idx)
        asins = [c["asin"] for c in data["future_age_cards"]]
        self.assertEqual(asins, ["B01BR000001", "B02BR000002", "B03BR000003", "B09OTHER009"])

    def test_series_tag_ranks_first_within_brand(self):
        idx = {
            "B01BEY00001": _entry("B01BEY00001", age=24, score=90, brand="タカラトミー"),
            "B02TOM00002": _entry("B02TOM00002", age=24, score=50, brand="タカラトミー", series=["トミカ"]),
        }
        data = _data(age="1歳〜", tags=("トミカ", "パズル"), brand="タカラトミー")
        bp._recommend_future_age(data, idx)
        self.assertEqual([c["asin"] for c in data["future_age_cards"]], ["B02TOM00002", "B01BEY00001"])

    def test_fewer_than_two_or_adult_is_empty(self):
        idx = {"B01ONLY0001": _entry("B01ONLY0001", age=24)}
        data = _data(age="1歳〜")
        bp._recommend_future_age(data, idx)
        self.assertEqual(data["future_age_cards"], [])
        idx2 = {f"B0{i}AD00000{i}": _entry(f"B0{i}AD00000{i}", age=230) for i in range(1, 4)}
        data2 = _data(age="18歳以上")
        bp._recommend_future_age(data2, idx2)
        self.assertEqual(data2["future_age_cards"], [])


class AgeExtractionTests(unittest.TestCase):
    def test_fallback_order(self):
        self.assertEqual(bp._resolve_age_min_months({"product": {"target_age": "1歳半〜"}}), 18)
        self.assertEqual(bp._resolve_age_min_months({"product": {}, "persona_fit": {"age_range": "3歳〜"}}), 36)
        self.assertEqual(bp._resolve_age_min_months(
            {"product": {}, "technical_specs": {"other": ["素材: 木", "対象年齢: 2歳以上"]}}), 24)
        self.assertEqual(bp._resolve_age_min_months({"product": {"name_full": "パズル 6歳以上"}}), 72)
        self.assertIsNone(bp._resolve_age_min_months({"product": {}}))
        # persona_fit が dict でなくても落ちない
        self.assertIsNone(bp._resolve_age_min_months({"product": {}, "persona_fit": None}))

    def test_seasonal_decoration_is_unknown(self):
        self.assertIsNone(bp._resolve_age_min_months({"product": {"name": "五月人形 兜飾り", "target_age": "0歳〜"}}))

    def test_index_and_front_matter_agree(self):
        art = {"slug": "2026-01-01-B0AGE000001", "tags": ["パズル"],
               "product": {"asin": "B0AGE000001", "name": "木のパズル", "age_range": "2歳〜",
                           "prices": {"amazon": {"price": 1200}}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "2026-01-01-B0AGE000001.json").write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")
            idx = bp._build_article_index(root, root / "per_asin")
            fm = bp._frontmatter_meta(dict(art, toy_types=idx["B0AGE000001"]["toy_types"]),
                                      art["slug"], False, {}, root / "2026-01-01-B0AGE000001.json")
        self.assertEqual(idx["B0AGE000001"]["age_min_months"], 24)
        self.assertEqual(fm["age_min_months"], 24)
        self.assertEqual(idx["B0AGE000001"]["toy_types"], ["puzzle"])
        self.assertEqual(fm["toy_types"], ["puzzle"])


if __name__ == "__main__":
    unittest.main()
