"""#9186 — 数字以外の年齢の手がかり (age_semantics) と、それを使う
build_feature_lists / build_category_hubs の読み取り。

build_post (front matter) 側は test_build_post_age_unknown.py。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from age_semantics import (  # type: ignore[import-not-found]  # noqa: E402
    ADULT_AGE_MONTHS,
    is_seasonal_decoration,
    min_months_from_words,
)
from build_category_hubs import build_min_months_index  # type: ignore[import-not-found]  # noqa: E402
from build_feature_lists import (  # type: ignore[import-not-found]  # noqa: E402
    age_min_months_from_article,
    parse_min_months,
)

# data/articles に実在する節句・正月の飾り物と、名前が似ているが遊び道具の物
DECORATIONS = (
    "五月人形 兜飾り クムキ 組",
    "クムキ 羽 ～はね～ つるし雛飾り",
    "薬師窯 五月人形 端午の節句 錦彩 鯉のぼりセット(土鈴)",
    "サンリオキャラクターズ 五月人形 端午の鯉飾り Moku",
    "人と木の木製鏡餅 クムキ 色（渋色）",
    "ぷりふあ 弓太刀 -SEI-",
    "室内用ミニこいのぼり(矢車・ポール付)",
    "平安義正 極上陣羽織 黒×金",
    "飾りミニ羽子板 新助六",
    "クムキ 色 三宝雛飾り",
    "木製節句飾り クムキ 色 羽子板",
)
PLAY_TOYS = (
    "CINECE 福笑い お正月遊びセット",
    "平和工業 MOCCO 木のホビーキット ひな祭り",
    "鉄芯こま ひも付き",
    "ことわざカードかるた",
    "へんがおならべ",
)


class MinMonthsFromWordsTests(unittest.TestCase):
    def test_words(self):
        for text, months in (
            ("小学生以上", 72),
            ("幼児（受験期）", 36),
            ("幼児〜小学生", 36),
            ("中学生以上", 144),
            ("高校生以上", 180),
            ("大人向け", ADULT_AGE_MONTHS),
            ("全年齢（大人用）", ADULT_AGE_MONTHS),
        ):
            with self.subTest(text=text):
                self.assertEqual(min_months_from_words(text), months)

    def test_not_age_words(self):
        for text in (None, "", "全年齢", "子供から大人まで", "小学生未満", "記載なし"):
            with self.subTest(text=text):
                self.assertIsNone(min_months_from_words(text))


class SeasonalDecorationTests(unittest.TestCase):
    def test_decorations(self):
        for name in DECORATIONS:
            with self.subTest(name=name):
                self.assertTrue(is_seasonal_decoration(name))

    def test_play_toys_are_not_decorations(self):
        for name in PLAY_TOYS:
            with self.subTest(name=name):
                self.assertFalse(is_seasonal_decoration(name))

    def test_any_name_field(self):
        self.assertTrue(is_seasonal_decoration(None, "", "吉利商事 鯉のぼり"))
        self.assertFalse(is_seasonal_decoration(None, ""))


class FeatureListsAgeTests(unittest.TestCase):
    def test_parse_min_months_reads_words(self):
        self.assertEqual(parse_min_months("小学生以上"), 72)
        self.assertEqual(parse_min_months("対象年齢の記載なし（小学生以上推奨）"), 72)
        self.assertEqual(parse_min_months("大人向け"), ADULT_AGE_MONTHS)
        self.assertIsNone(parse_min_months("対象年齢の記載なし"))
        self.assertEqual(parse_min_months("3歳以上"), 36)

    def test_seasonal_decoration_has_no_age(self):
        raw = {"product": {"name": "吉利商事 鯉のぼり", "target_age": "0歳〜"}}
        self.assertIsNone(age_min_months_from_article(raw))
        raw = {"product": {"name": "CINECE 福笑い お正月遊びセット", "target_age": "3歳〜"}}
        self.assertEqual(age_min_months_from_article(raw), 36)


class CategoryHubAgeIndexTests(unittest.TestCase):
    def test_seasonal_decorations_are_not_in_age_hubs(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            for asin, name in (("B000000001", "吉利商事 鯉のぼり"), ("B000000002", "ラトル")):
                (d / f"{asin}.json").write_text(json.dumps({
                    "product": {"asin": asin, "name": name},
                    "persona_fit": {"age_range": "0歳〜"},
                }, ensure_ascii=False), encoding="utf-8")
            index = build_min_months_index(d)
        self.assertEqual(index, {"B000000002": 0})


if __name__ == "__main__":
    unittest.main()
