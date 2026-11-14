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
from datetime import date, timedelta

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

    def test_all_of_second_path(self):
        # #3654: montessori は明示語が無くても「木製 AND 教具形態」で合格する。
        theme = bch.THEMES["montessori"]
        self.assertTrue(bch._matches("木製 の 型はめ パズル", theme))
        # 形態だけ / 素材だけでは all_of 不成立。
        self.assertFalse(bch._matches("プラスチックの型はめ", theme))
        self.assertFalse(bch._matches("木製の楽器", theme))

    def test_include_beats_missing_all_of(self):
        # 明示語 (include) 単独で合格 = all_of を満たさなくてもよい。
        self.assertTrue(bch._matches("モンテッソーリ の 教材", bch.THEMES["montessori"]))

    def test_montessori_deny_veto(self):
        # 木製 AND 教具形態でも DENY 語 (量産プラ/電子) があれば fail-closed。
        theme = bch.THEMES["montessori"]
        self.assertFalse(bch._matches("木製 型はめ レゴ 互換", theme))
        self.assertFalse(bch._matches("木製 型はめ 電子 メロディ", theme))

    def test_shape_include_and_deny(self):
        # #2687/#2690: 図形・空間 hub。図形語で合格、キャラ/乗り物 DENY で veto。
        theme = bch.THEMES["shape"]
        self.assertTrue(bch._matches("マグ・フォーマー 空間認識 図形", theme))
        self.assertTrue(bch._matches("タングラム 立体パズル", theme))
        # 妖怪ウォッチ立体パズル等のキャラ立体は編集品質のため除外。
        self.assertFalse(bch._matches("妖怪ウォッチ 立体パズル", theme))
        self.assertFalse(bch._matches("レゴ 図形 セット", theme))
        # shape は min_ivs override を持たない (供給潤沢で既定 3.8 で足りる)。
        self.assertNotIn("min_ivs", bch.THEMES["shape"])

    def test_music_include_and_deny(self):
        # brain#67: 楽器・リズム hub。キャラ楽器はこの棚の需要の中心なので残す。
        theme = bch.THEMES["music"]
        self.assertTrue(bch._matches("アンパンマン うちの子天才 シロホン 楽器", theme))
        self.assertTrue(bch._matches("ハペ ベビードラム", theme))
        self.assertTrue(bch._matches("リズム遊び マラカス", theme))
        # 「ドラム」がナーフのドラムマガジンに、「ピアノ」が大人向けレゴに当たる。
        self.assertFalse(bch._matches("ナーフ エリート ドラムマガジン", theme))
        self.assertFalse(bch._matches("レゴ アイデア グランドピアノ", theme))
        # 英語うたマイク等は英語 hub の担当。
        self.assertFalse(bch._matches("くもんの英語うたマイク リズム", theme))
        # 「音楽が鳴る」は電子玩具の説明文に広く出るため include に入れない。
        self.assertFalse(bch._matches("音楽が鳴る 車のおもちゃ", theme))


class ThemeMinIvsOverrideTest(unittest.TestCase):
    def test_montessori_override_present(self):
        # #3654: montessori だけ 3.4 に緩和され、他テーマは既定を継承する。
        self.assertEqual(bch.THEMES["montessori"]["min_ivs"], 3.4)
        self.assertNotIn("min_ivs", bch.THEMES["english"])

    def test_run_uses_theme_min_ivs(self):
        # ivs=3.5 の木製教具は既定 3.8 では落ち、montessori override 3.4 で拾われる。
        records = [_rec("W1", 3.5, 70, 1200)]
        text = {"W1": "木製 の 紐通し おもちゃ"}
        theme = bch.THEMES["montessori"]
        got = bch.build_hub(records, text, theme,
                            top_n=10, min_ivs=theme.get("min_ivs", 3.8))
        self.assertEqual([r.asin for r in got], ["W1"])
        # 既定 3.8 なら同じ record は落ちる (override が効いている証明)。
        dropped = bch.build_hub(records, text, theme, top_n=10, min_ivs=3.8)
        self.assertEqual(dropped, [])


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
        # #3364: url_internal は /products/{asin}/ 形式 (slug ではなく asin)。
        self.assertEqual(it["url_internal"], "/products/abc/")  # lowercased


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


class AgeHubBrandCapTest(unittest.TestCase):
    """年齢 hub の同一メーカー上限 (navi-brain#95)。"""

    def setUp(self):
        self.hub = bch.AGE_HUBS["age-1"]
        # スコア順: X1 X2 X3 (メーカー X) > Y1 > Z1 > N1 (brand 不明)
        self.records = [
            _rec("X1", 4.9, 99, 1000),
            _rec("X2", 4.9, 98, 1000),
            _rec("X3", 4.9, 97, 1000),
            _rec("Y1", 4.5, 90, 1000),
            _rec("Z1", 4.4, 88, 1000),
            _rec("N1", 4.3, 86, 1000),
        ]
        self.mm = {r.asin: 12 for r in self.records}
        self.brands = {"X1": "X", "X2": "X", "X3": "X", "Y1": "Y", "Z1": "Z"}

    def _asins(self, **kw):
        return [r.asin for r in bch.build_age_hub(
            self.records, self.mm, self.hub, min_ivs=3.8, **kw)]

    def test_no_cap_by_default(self):
        self.assertEqual(self._asins(top_n=3), ["X1", "X2", "X3"])

    def test_cap_spreads_brands_in_score_order(self):
        out = self._asins(top_n=3, brand_index=self.brands, max_per_brand=1)
        self.assertEqual(out, ["X1", "Y1", "Z1"])

    def test_unknown_brand_is_never_capped(self):
        out = self._asins(top_n=4, brand_index=self.brands, max_per_brand=1)
        self.assertEqual(out, ["X1", "Y1", "Z1", "N1"])

    def test_backfills_when_brands_run_out(self):
        # 上限で外した X2 を戻して top_n を満たし、並びはスコア順に戻す
        out = self._asins(top_n=5, brand_index=self.brands, max_per_brand=1)
        self.assertEqual(out, ["X1", "X2", "Y1", "Z1", "N1"])

    def test_zero_disables_cap(self):
        out = self._asins(top_n=3, brand_index=self.brands, max_per_brand=0)
        self.assertEqual(out, ["X1", "X2", "X3"])


class BuildBrandIndexTest(unittest.TestCase):
    def _write(self, adir, asin, brand):
        doc = {"slug": asin, "product": {"asin": asin, "brand": brand}}
        (adir / f"2026-06-01-{asin}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def test_variants_share_a_key_and_unknowns_stay_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp)
            self._write(adir, "B0LEGO0001", "レゴ(LEGO)")
            self._write(adir, "B0LEGO0002", "LEGO")
            self._write(adir, "B0UNKN0001", "Zzqx Toys")
            self._write(adir, "B0UNKN0002", "Qqwv Works")
            self._write(adir, "B0EMPTY001", "")
            idx = bch.build_brand_index(adir)
        self.assertEqual(idx["B0LEGO0001"], idx["B0LEGO0002"])
        # taxonomy に無いメーカー同士を「ノーブランド」1 枠にまとめない
        self.assertNotEqual(idx["B0UNKN0001"], idx["B0UNKN0002"])
        self.assertNotIn("B0EMPTY001", idx)


class RunAgeHubBrandCapTest(unittest.TestCase):
    def test_capped_items_move_to_lineup(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()
            for i, score in enumerate([4.9, 4.8, 4.7]):
                asin = f"B0LEGOCAP{i}"
                doc = {
                    "slug": asin, "title": "t",
                    "persona_fit": {"age_range": "1歳〜"},
                    "product": {"asin": asin, "name": asin, "brand": "レゴ",
                                "ivs_score": score, "best_price": 1000,
                                "best_platform": "amazon",
                                "prices": {"amazon": {"url": "https://amazon/x"}}},
                }
                (adir / f"2026-06-01-{asin}.json").write_text(
                    json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            out = pathlib.Path(tmp) / "features"
            bch.run(adir, out, top_n=2, min_ivs=0.0, themes=[],
                    age_hubs=["age-1"], age_max_per_brand=1)
            data = json.loads((out / "age-1.json").read_text(encoding="utf-8"))
        # メーカーが 1 社しか無い bucket は、件数を減らさず埋め戻す
        self.assertEqual([i["asin"] for i in data["items"]],
                         ["B0LEGOCAP0", "B0LEGOCAP1"])
        self.assertEqual([i["asin"] for i in data["lineup"]], ["B0LEGOCAP2"])


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


class BuildAgeLineupTest(unittest.TestCase):
    """Slice A: 年齢hub 全ラインナップ節 (min_ivs ゲート無し・top-24 除外)。"""

    def setUp(self):
        # 1歳 hub = [12, 24)
        self.hub = bch.AGE_HUBS["age-1"]
        self.records = [
            _rec("A", 4.5, 90, 2000),
            _rec("B", 4.8, 95, 3000),
            _rec("C", 1.0, 20, 1000),   # low ivs_score but still bucket member
            _rec("D", 4.0, 95, 1500),   # ties B, cheaper
            _rec("E", 4.2, 80, 500),    # out of age range
            _rec("F", 4.2, 80, 700),    # no age data
        ]
        self.mm = {"A": 12, "B": 18, "C": 12, "D": 23, "E": 36, "F": None}

    def test_no_min_ivs_gate_low_score_included(self):
        out = bch.build_age_lineup(self.records, self.mm, self.hub, exclude_asins=set())
        asins = [r.asin for r in out]
        self.assertIn("C", asins)  # low ivs_score is NOT dropped (crawl discovery)
        self.assertNotIn("E", asins)  # still gated by age range
        self.assertNotIn("F", asins)  # still gated by missing age data

    def test_excludes_top24_asins(self):
        out = bch.build_age_lineup(self.records, self.mm, self.hub,
                                   exclude_asins={"D", "B"})
        asins = [r.asin for r in out]
        self.assertNotIn("D", asins)
        self.assertNotIn("B", asins)
        self.assertIn("A", asins)
        self.assertIn("C", asins)

    def test_sort_ivs_desc_price_asc_tiebreak(self):
        out = bch.build_age_lineup(self.records, self.mm, self.hub, exclude_asins=set())
        self.assertEqual([r.asin for r in out], ["D", "B", "A", "C"])


class LineupEntryTest(unittest.TestCase):
    def test_minimal_fields_and_lowercased_url(self):
        rec = _rec("AbC1", 4.5, 90, 2000)
        entry = bch._lineup_entry(rec, 18)
        self.assertEqual(entry, {
            "asin": "AbC1",
            "name": "name-AbC1",
            "url_internal": "/products/abc1/",
            "ivs_100": 90,
            "age_min_months": 18,
        })


class SerializeHubLineupTest(unittest.TestCase):
    def test_lineup_key_present_when_given(self):
        items = [_rec("A", 4.5, 90, 2000)]
        lineup = [bch._lineup_entry(_rec("B", 3.0, 40, 500), 12)]
        payload = bch.serialize_hub(items, "age-1", "2026-07-20T00:00:00Z",
                                    lineup=lineup)
        self.assertEqual(payload["lineup"], lineup)

    def test_lineup_key_absent_when_omitted(self):
        items = [_rec("A", 4.5, 90, 2000)]
        payload = bch.serialize_hub(items, "english", "2026-07-20T00:00:00Z")
        self.assertNotIn("lineup", payload)


class RunAgeHubLineupTest(unittest.TestCase):
    def test_lineup_holds_top_n_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()

            def _doc(asin, price):
                return {
                    "slug": f"toy-{asin}",
                    "title": "1歳のおもちゃ",
                    "persona_fit": {"age_range": "1歳〜"},
                    "product": {
                        "asin": asin,
                        "name": f"name-{asin}",
                        "ivs_score": 4.5,
                        "best_price": price,
                        "best_platform": "amazon",
                        "prices": {"amazon": {"url": f"https://amazon/{asin}"}},
                    },
                }

            # 2 商品を投入し top_n=1 で強制的に 1 件だけ curated 入りさせる。
            # price だけ差をつけて build_age_hub の同点タイブレーク (安価優先) で
            # B0TOP1 (安い) が items に残り、B0TAIL1 が lineup に落ちる想定。
            for asin, price in [("B0TOP1", 1000), ("B0TAIL1", 5000)]:
                (adir / f"2026-06-01-{asin}.json").write_text(
                    json.dumps(_doc(asin, price), ensure_ascii=False),
                    encoding="utf-8")

            out = pathlib.Path(tmp) / "features"
            bch.run(adir, out, top_n=1, min_ivs=0.0, themes=[], age_hubs=["age-1"])
            data = json.loads((out / "age-1.json").read_text(encoding="utf-8"))
            self.assertEqual([it["asin"] for it in data["items"]], ["B0TOP1"])
            lineup_asins = [e["asin"] for e in data["lineup"]]
            self.assertEqual(lineup_asins, ["B0TAIL1"])
            for entry in data["lineup"]:
                self.assertEqual(
                    set(entry.keys()),
                    {"asin", "name", "url_internal", "ivs_100", "age_min_months"})


class HubIndexTest(unittest.TestCase):
    def test_write_hub_index_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            entries = [{"key": "english", "label": "英語", "url": "/english-toys/"}]
            bch._write_hub_index(out, entries)
            data = json.loads((out / "hub_index.json").read_text(encoding="utf-8"))
            self.assertEqual(data, entries)

    def test_run_writes_hub_index_for_themes_and_age_hubs(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()
            doc = {
                "slug": "eigo-toy",
                "title": "英語のおもちゃ",
                "tags": ["英語"],
                "persona_fit": {"age_range": "1歳〜"},
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
            bch.run(adir, out, top_n=24, min_ivs=0.0,
                   themes=["english"], age_hubs=["age-1"])
            hub_index = json.loads((out / "hub_index.json").read_text(encoding="utf-8"))
            self.assertEqual(hub_index, [
                {"key": "english", "label": "英語", "url": "/english-toys/"},
                {"key": "age-1", "label": "1歳", "url": "/toys-age-1/"},
            ])



class RankingHistoryTests(unittest.TestCase):
    def test_window_and_year_boundary_deduplicate_days(self):
        # history は挿入順 (= 古い順)。12-01 は窓 (30 日) の外、01-01 の重複は 1 日として最高順位を取る。
        history = {"A": {"history": [
            {"d": "12-01", "r": 2}, {"d": "12-31", "r": 12},
            {"d": "01-01", "r": 8}, {"d": "01-01", "r": 5},
        ]}}
        self.assertEqual(bch.rank_metrics(history, "A", date(2026, 1, 2)), (2, 5))

    def test_priority_and_serialization(self):
        low_score = _rec("A", 3.8, 75, 1000)
        high_score = _rec("B", 4.5, 90, 1000)
        ordinary = _rec("C", 4.9, 99, 1000)
        history = {"A": {"history": [{"d": f"01-{n:02d}", "r": 20} for n in (1, 2, 3)]}}
        items = bch.prioritize_ranked([high_score, ordinary, low_score], history, date(2026, 1, 3))
        self.assertEqual([x.asin for x in items], ["A", "B", "C"])
        payload = bch.serialize_hub(items, "math", "now")
        self.assertEqual(payload["items"][0]["rank_days_30"], 3)
        self.assertEqual(payload["items"][0]["best_rank_30"], 20)


class NewArrivalsTests(unittest.TestCase):
    def test_first_last_seen_and_year_boundary(self):
        history = {"A": {"history": [{"d": "12-28", "r": 8}, {"d": "01-02", "r": 5}, {"d": "01-05", "r": 3}]}}
        self.assertEqual(bch.first_last_seen(history, "A", date(2027, 1, 5)),
                         (date(2026, 12, 28), date(2027, 1, 5)))

    def test_release_date_patterns(self):
        for title in ("【2026年11月21日発売 予約商品】Tamagotchi", "Tamagotchi ｜【予約 2026/11/21】発売日前日出荷｜"):
            self.assertEqual(bch.parse_release_date(title), date(2026, 11, 21))
        for title in ("【ご予約 11月中旬発送予定】", "[2026年11月21日出荷予定] タカラトミー", "2026年2月30日発売",
                      # 予約が付いていても、出荷日・発送日・予約の受付開始日は発売日ではない
                      "【予約】2026年11月21日出荷予定", "【予約商品】2026/11/21発送予定", "予約開始 2026/11/01",
                      "【予約受付中】2026/11/30まで",
                      # 型番などの長い数字の一部には当てない
                      "【予約】型番2026-11-210", "JAN 12026-11-21発売"):
            self.assertIsNone(bch.parse_release_date(title), title)
        # 「発売」が出荷より先なら発売日として採る
        self.assertEqual(bch.parse_release_date("【予約 2026/11/21】発売日前日出荷"), date(2026, 11, 21))

    def test_window_boundaries(self):
        # 初登場は今日を含む 30 日 (29 日前まで)、最終観測は今日を含む 7 日 (6 日前まで)
        today = date(2026, 11, 14)
        cases = {"F29": (29, 1), "F30": (30, 1), "L6": (10, 6), "L7": (10, 7)}
        records, history, ages = [], {}, {}
        for asin, (first_days, last_days) in cases.items():
            records.append(_rec(asin, 4.5, 80, 1000))
            history[asin] = {"history": [
                {"d": (today - timedelta(days=first_days)).strftime("%m-%d"), "r": 5},
                {"d": (today - timedelta(days=last_days)).strftime("%m-%d"), "r": 5},
            ]}
            ages[asin] = 36
        chosen = {r.asin for r in bch.select_new_arrivals(records, history, ages, {}, today=today)}
        self.assertEqual(chosen, {"F29", "L6"})

    def test_release_info_in_payload(self):
        today = date(2026, 11, 14)
        names = {"UP": "【2026年11月21日発売 予約商品】A", "RECENT": "2026年11月1日発売 B",
                 "OLD": "2026年9月1日発売 C", "NONE": "D"}
        records = [_rec(a, 4.5, 80, 1000) for a in names]
        history = {a: {"name": n, "history": [{"d": "11-10", "r": 3}, {"d": "11-13", "r": 3}]}
                   for a, n in names.items()}
        ages = {a: 36 for a in names}
        payload = {p["asin"]: p for p in bch._new_arrivals_payload(
            bch.select_new_arrivals(records, history, ages, {}, today=today))}
        self.assertEqual((payload["UP"]["release_label"], payload["UP"]["release_upcoming"]), ("11/21", True))
        self.assertEqual((payload["RECENT"]["release_label"], payload["RECENT"]["release_upcoming"]), ("11/1", False))
        self.assertNotIn("release_label", payload["OLD"])  # 30 日より前の発売は出さない
        self.assertNotIn("release_label", payload["NONE"])
        self.assertEqual(payload["UP"]["first_seen_label"], "11/10")

    def test_selection_windows_score_age_brand_and_limit(self):
        today = date(2026, 11, 14)
        records, history, ages, brands = [], {}, {}, {}
        for n in range(9):
            asin = f"A{n}"
            records.append(_rec(asin, 4.5, 61 if n != 8 else 60, 1000))
            first = (today - timedelta(days=10 if n < 6 else 40)).strftime("%m-%d")
            last = (today - timedelta(days=10 if n == 6 else 1)).strftime("%m-%d")
            history[asin] = {"history": [{"d": first, "r": 10}, {"d": last, "r": n + 1}]}
            ages[asin] = 36 if n != 7 else 72
            brands[asin] = "same" if n < 4 else f"brand{n}"
        del ages["A5"]
        chosen = bch.select_new_arrivals(records, history, ages, brands, today=today)
        # 上限内は A0, A1 (same) と A4 の 3 件。MIN (4) に足りないので A2 を 1 件だけ戻す
        self.assertEqual([r.asin for r in chosen], ["A0", "A1", "A2", "A4"])
        self.assertNotIn("A6", [r.asin for r in chosen])
        self.assertNotIn("A7", [r.asin for r in chosen])
        self.assertNotIn("A8", [r.asin for r in chosen])
        self.assertEqual(len(bch.select_new_arrivals(records, history, ages, brands, today=today,
                                                     age_range=(0, 36))), 0)

    def test_first_seen_and_last_seen_boundaries_and_maximum(self):
        today = date(2026, 11, 14)
        records, history, ages, brands = [], {}, {}, {}
        for n in range(10):
            asin = f"M{n}"
            records.append(_rec(asin, 4.5, 70, 1000))
            first_days = 10 if n < 8 else 20
            last_days = 10 if n >= 8 else 1
            history[asin] = {"history": [
                {"d": (today - timedelta(days=first_days)).strftime("%m-%d"), "r": n + 1},
                {"d": (today - timedelta(days=last_days)).strftime("%m-%d"), "r": n + 1},
            ]}
            ages[asin] = 36 if n < 8 else 48
            brands[asin] = f"brand{n}"
        # M8 は初登場が範囲内でも最終掲載が10日前なので除外。M9 は同様に除外。
        result = bch.select_new_arrivals(records, history, ages, brands, today=today)
        self.assertEqual(len(result), bch.NEW_ARRIVAL_MAX)
        self.assertEqual([r.asin for r in result], [f"M{n}" for n in range(6)])
        # 20日前に初登場した商品は期間内、40日前なら期間外。
        history["M8"]["history"][-1]["d"] = (today - timedelta(days=1)).strftime("%m-%d")
        history["M9"]["history"][0]["d"] = (today - timedelta(days=40)).strftime("%m-%d")
        self.assertNotIn("M8", [r.asin for r in bch.select_new_arrivals(records, history, ages, brands, today=today)])

    def test_brand_cap_backfills_only_up_to_minimum(self):
        # 同じメーカーばかりで上限 2 件だと MIN (4) に届かない → 外した品を MIN まで戻す
        today = date(2026, 11, 14)
        records = [_rec(f"B{i}", 4.5, 80, 1000) for i in range(6)]
        history = {f"B{i}": {"history": [{"d": f"11-{10 - i:02d}", "r": i + 1}, {"d": "11-13", "r": i + 1}]}
                   for i in range(6)}
        ages = {f"B{i}": 36 for i in range(6)}
        brands = {f"B{i}": "bandai" for i in range(5)}
        brands["B5"] = "other"
        chosen = bch.select_new_arrivals(records, history, ages, brands, today=today)
        # 上限内: B0, B1 (bandai) と B5 (other) の 3 件 → B2 を 1 件だけ戻し、初登場の新しい順に並べる
        self.assertEqual([r.asin for r in chosen], ["B0", "B1", "B2", "B5"])

    def test_minimum_age_range(self):
        today = date(2026, 11, 14)
        records = [_rec(str(i), 4.5, 80, 1000) for i in range(4)]
        history = {str(i): {"history": [{"d": "11-04", "r": i + 1}, {"d": "11-13", "r": i + 1}]}
                   for i in range(4)}
        ages = {"0": 0, "1": 11, "2": 12, "3": 24}
        selected = bch.select_new_arrivals(records, history, ages, {}, today=today, age_range=(0, 12))
        self.assertEqual([r.asin for r in selected], ["0", "1"])

    def test_run_writes_new_arrivals_empty_when_history_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            adir = pathlib.Path(tmp) / "articles"
            adir.mkdir()
            out = pathlib.Path(tmp) / "features"
            counts = bch.run(adir, out, top_n=24, min_ivs=0.0, themes=[], age_hubs=[], today=date(2026, 11, 14))
            data = json.loads((out / "new_arrivals.json").read_text(encoding="utf-8"))
            self.assertEqual(data["top"], [])
            self.assertEqual(data["age"], {})
            self.assertEqual(counts["new_arrivals_top"], 0)


if __name__ == "__main__":
    unittest.main()
