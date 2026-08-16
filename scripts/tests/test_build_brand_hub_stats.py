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


class TestBrandNameVariants(unittest.TestCase):
    """brand_normalizer.name_variants (#5343 follow-up)。

    _series_candidates の挙動は上のクラスで見るので、ここは API 単体の契約
    (canonical 自身を含む / 未登録は空 / 傘下シリーズは含まない) だけを見る。
    """

    def test_includes_canonical_itself(self):
        from brand_normalizer import _fold, name_variants
        self.assertIn(_fold("BabyBus"), name_variants("BabyBus"))

    def test_includes_declared_variants(self):
        from brand_normalizer import _fold, name_variants
        self.assertIn(_fold("ベビーバス"), name_variants("BabyBus"))

    def test_excludes_series_aliases(self):
        from brand_normalizer import _fold, name_variants
        # トミカは alias だが name_variants ではない (実在シリーズ名)
        self.assertNotIn(_fold("トミカ"), name_variants("タカラトミー"))

    def test_unknown_brand_is_empty(self):
        from brand_normalizer import name_variants
        self.assertEqual(name_variants("存在しないブランド名XYZ"), frozenset())
        self.assertEqual(name_variants(None), frozenset())


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

    def test_filters_brand_name_reappearing_later(self):
        # #5322: tags[0] 以外の位置に再出現したブランド名を落とす。
        out = bhs._series_candidates(
            ["ミニカー", "デュプロ", "レゴ"], "レゴ"
        )
        self.assertEqual(out, ["デュプロ"])

    def test_drops_declared_name_variants(self):
        # #5343 follow-up: brand_taxonomy.yaml の name_variants に書かれた
        # 「同じ名前の別表記」は落とす。BabyBus の "ベビーバス" が実データの例。
        out = bhs._series_candidates(
            ["はじめてのちいくずかん", "ベビータッチペン", "ベビーバス"], "BabyBus"
        )
        self.assertEqual(out, ["ベビータッチペン"])

    def test_keeps_real_series_that_are_also_aliases(self):
        # name_variants を見るようにしても、alias に入っている**実在シリーズ名**は
        # 残らなければならない。ここが崩れると #5343 follow-up の変更は失敗。
        for canonical, series in (
            ("タカラトミー", "トミカ"),
            ("カワダ", "ナノブロック"),
            ("ハズブロ", "ナーフ"),
            ("ヨシリツ", "ラキュー"),
            ("学研", "ニューブロック"),
        ):
            with self.subTest(canonical=canonical):
                out = bhs._series_candidates(["ミニカー", series], canonical)
                self.assertIn(series, out)

    def test_keeps_series_registered_as_brand_alias(self):
        # 「プラレール」はタカラトミーの alias でもあるが実在のシリーズ名。
        # canonical 一致で落とすと消えてしまうので、表記一致でのみ落とす。
        out = bhs._series_candidates(
            ["ミニカー", "プラレール", "タカラトミー"], "タカラトミー"
        )
        self.assertEqual(out, ["プラレール"])

    def test_brand_name_match_is_case_and_width_insensitive(self):
        out = bhs._series_candidates(["X", "デュプロ", "ＬＥＧＯ"], "LEGO")
        self.assertEqual(out, ["デュプロ"])

    def test_canonical_omitted_keeps_previous_behaviour(self):
        # canonical 未指定なら従来どおり tags[0] しか落とさない。
        out = bhs._series_candidates(["ミニカー", "プラレール", "タカラトミー"])
        self.assertEqual(out, ["プラレール", "タカラトミー"])


class TestAgeBandLabel(unittest.TestCase):
    def test_under_one_year(self):
        self.assertEqual(bhs.age_band_label(10.0), "0歳")

    def test_one_year_band(self):
        self.assertEqual(bhs.age_band_label(18.0), "1歳")

    def test_floors_to_year(self):
        # 80ヶ月 = 6.67歳 -> 偽の精度を出さず 6歳 に丸める
        self.assertEqual(bhs.age_band_label(80.0), "6歳")

    def test_exact_year_boundary(self):
        self.assertEqual(bhs.age_band_label(24), "2歳")

    def test_none_and_negative(self):
        self.assertIsNone(bhs.age_band_label(None))
        self.assertIsNone(bhs.age_band_label("3歳"))
        self.assertIsNone(bhs.age_band_label(-1))


class TestSeoTitle(unittest.TestCase):
    def test_large_brand_shows_count(self):
        t = bhs.build_seo_title("タカラトミー", {
            "count": 61, "top_3_series": ["ミニカー"], "avg_age_min_months": 36.0,
        })
        self.assertEqual(t, "タカラトミーの知育玩具61選｜スコアと価格で比較")

    def test_small_brand_hides_count(self):
        # #5322: 少ない件数を title に出すと「3 件しかない」の自己申告になる
        t = bhs.build_seo_title("4M", {
            "count": 3, "top_3_series": ["実験キット"], "avg_age_min_months": 80.0,
        })
        self.assertNotIn("3", t)
        self.assertEqual(t, "4Mの知育玩具レビュー｜6歳から選ぶ比較ガイド")

    def test_threshold_boundary(self):
        entry = {"count": bhs.TITLE_COUNT_MIN, "top_3_series": [],
                 "avg_age_min_months": 36.0}
        self.assertIn(str(bhs.TITLE_COUNT_MIN), bhs.build_seo_title("B", entry))
        entry["count"] = bhs.TITLE_COUNT_MIN - 1
        self.assertNotIn(str(bhs.TITLE_COUNT_MIN - 1), bhs.build_seo_title("B", entry))

    def test_falls_back_to_series_then_generic(self):
        self.assertEqual(
            bhs.build_seo_title("B", {"count": 3, "top_3_series": ["S1"]}),
            "Bの知育玩具レビュー｜S1をスコアで比較",
        )
        self.assertEqual(
            bhs.build_seo_title("B", {"count": 3, "top_3_series": []}),
            "Bの知育玩具レビュー｜スコアと価格で比較",
        )


class TestSeoDescription(unittest.TestCase):
    def _d(self, count, series, age=36.0):
        return bhs.build_seo_description("B", {
            "count": count, "top_3_series": series, "avg_age_min_months": age,
        })

    def test_never_contains_price(self):
        # #5322: best_price は日次更新、meta は full rebuild 時点で凍るため
        # SERP と実ページが食い違う。価格は description に入れない。
        d = bhs.build_seo_description("B", {
            "count": 5, "top_3_series": ["S1"], "avg_age_min_months": 36.0,
            "lowest_price": 1980,
        })
        self.assertNotIn("1980", d)
        self.assertNotIn("円", d)

    def test_includes_series_and_age(self):
        d = self._d(5, ["デュプロ", "クラシック"])
        self.assertIn("デュプロ", d)
        self.assertIn("クラシック", d)
        self.assertIn("3歳", d)

    def test_uses_at_most_two_series(self):
        d = self._d(5, ["S1", "S2", "S3"])
        self.assertNotIn("S3", d)

    def test_count_bands_differ(self):
        # 件数帯ごとに訴求が変わる (同じ 1 文型を全ブランドで使わない)
        small, mid, large = self._d(3, ["S1"]), self._d(12, ["S1"]), self._d(40, ["S1"])
        self.assertEqual(len({small, mid, large}), 3)

    def test_survives_missing_series_and_age(self):
        d = bhs.build_seo_description("B", {"count": 4, "top_3_series": [],
                                            "avg_age_min_months": None})
        self.assertTrue(d.startswith("Bの知育玩具4件"))
        self.assertNotIn("〜の商品", d)


class TestRelatedBrands(unittest.TestCase):
    def _brands(self, **overrides) -> dict:
        base = {
            "A": {"count": 5, "tier": "B", "avg_age_min_months": 36.0,
                  "top_3_series": ["ブロック玩具", "のりもの"]},
            "B": {"count": 5, "tier": "B", "avg_age_min_months": 36.0,
                  "top_3_series": ["ブロック玩具"]},
            "C": {"count": 5, "tier": "D", "avg_age_min_months": 96.0,
                  "top_3_series": ["実験キット"]},
        }
        base.update(overrides)
        return base

    def test_shared_series_beats_age_and_tier(self):
        brands = self._brands()
        # D: シリーズ共有なし・年齢も tier も一致
        brands["D"] = {"count": 99, "tier": "B", "avg_age_min_months": 36.0,
                       "top_3_series": ["ぬり絵"]}
        bhs.build_related_brands(brands)
        names = [r["name"] for r in brands["A"]["related_brands"]]
        self.assertEqual(names[0], "B")

    def test_reason_prefers_shared_series(self):
        brands = self._brands()
        bhs.build_related_brands(brands)
        rel = brands["A"]["related_brands"][0]
        self.assertEqual(rel["reason"], "ブロック玩具つながり")

    def test_never_links_to_noindex_brand(self):
        brands = self._brands()
        brands["B"]["noindex"] = True
        bhs.build_related_brands(brands)
        names = [r["name"] for r in brands["A"]["related_brands"]]
        self.assertNotIn("B", names)

    def test_never_links_to_thin_brand(self):
        brands = self._brands()
        brands["B"]["count"] = 2
        bhs.build_related_brands(brands)
        names = [r["name"] for r in brands["A"]["related_brands"]]
        self.assertNotIn("B", names)

    def test_never_links_to_self(self):
        brands = self._brands()
        bhs.build_related_brands(brands)
        for name, entry in brands.items():
            self.assertNotIn(name, [r["name"] for r in entry["related_brands"]])

    def test_caps_at_max(self):
        brands = {
            f"B{i}": {"count": 5, "tier": "B", "avg_age_min_months": 36.0,
                      "top_3_series": ["ブロック玩具"]}
            for i in range(8)
        }
        bhs.build_related_brands(brands)
        self.assertEqual(
            len(brands["B0"]["related_brands"]), bhs.RELATED_BRANDS_MAX
        )

    def test_thin_brand_gets_no_related_key(self):
        brands = self._brands()
        brands["T"] = {"count": 2}
        bhs.build_related_brands(brands)
        self.assertNotIn("related_brands", brands["T"])

    def test_deterministic_order_on_score_tie(self):
        brands = {
            "A": {"count": 5, "tier": "B", "avg_age_min_months": 36.0,
                  "top_3_series": ["S"]},
            "Y": {"count": 5, "tier": "B", "avg_age_min_months": 36.0,
                  "top_3_series": ["S"]},
            "X": {"count": 9, "tier": "B", "avg_age_min_months": 36.0,
                  "top_3_series": ["S"]},
        }
        bhs.build_related_brands(brands)
        # 同スコアなら記事数の多い方を先に
        self.assertEqual(
            [r["name"] for r in brands["A"]["related_brands"]], ["X", "Y"]
        )

    def test_unrelated_brand_is_dropped(self):
        brands = {
            "A": {"count": 5, "tier": "B", "avg_age_min_months": 12.0,
                  "top_3_series": ["S1"]},
            "Z": {"count": 5, "tier": "S", "avg_age_min_months": 120.0,
                  "top_3_series": ["S2"]},
        }
        bhs.build_related_brands(brands)
        self.assertEqual(brands["A"]["related_brands"], [])


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
        # #5322: seo_* は count >= 3 のときだけ出る
        self.assertIn("レゴ", rec["seo_title"])
        self.assertIn("デュプロ", rec["seo_description"])

    def test_seo_fields_absent_under_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上", ["レゴ", "デュプロ"]),
                _article("A2", "レゴ(LEGO)", 85, 2000, "5歳〜", ["レゴ", "クラシック"]),
            ])
            payload = bhs.aggregate(root)
        # noindex なので description は不要 (JSON を軽く保つ)
        self.assertNotIn("seo_title", payload["brands"]["レゴ"])
        self.assertNotIn("seo_description", payload["brands"]["レゴ"])

    def test_top_series_drops_substring_duplicates(self):
        # #5322: 「プログラミングおもちゃ」と「プログラミング」で 2 枠使わない
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "articles"
            _write_articles(root, [
                _article("A1", "レゴ(LEGO)", 80, 1000, "3歳以上",
                         ["レゴ", "プログラミングおもちゃ", "プログラミング", "ロボット"]),
                _article("A2", "レゴ(LEGO)", 85, 2000, "5歳〜",
                         ["レゴ", "プログラミングおもちゃ", "プログラミング", "ロボット"]),
                _article("A3", "レゴ(LEGO)", 85, 999, "1歳半〜",
                         ["レゴ", "プログラミングおもちゃ", "プログラミング", "ロボット"]),
            ])
            payload = bhs.aggregate(root)
        self.assertEqual(
            payload["brands"]["レゴ"]["top_3_series"],
            ["プログラミングおもちゃ", "ロボット"],
        )

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
