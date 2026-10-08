"""#9186 — 対象年齢「不明」を age_min_months=0（0ヶ月〜）と区別する。

  - 年齢の読めない表記 (空・「対象年齢の記載なし」「全年齢」) は不明。
    front matter に age_min_months を書かない (search.json では null になる)
  - 「0ヶ月〜」「0歳〜」のように 0 と書かれたものだけを 0 にする
  - 数字の無い「小学生以上」「大人向け」は言葉で読む (72 / 216)
  - 節句・正月の飾り物は年齢を書かない (「0歳〜」は初節句の赤ちゃんの意味)
  - _parse_age_min_months (不明も 0 を返す旧 API) は不明で 0 を返し続ける
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import (  # type: ignore[import-not-found]
    _frontmatter_meta,
    _get_development_stage,
    _parse_age_min_months,
    _parse_age_min_months_or_none,
)

UNKNOWN = (None, "", "  ", "対象年齢の記載なし", "記載なし", "全年齢", "指定なし", "子供から大人まで",
           "小学生未満")


class ParseAgeMinMonthsOrNoneTests(unittest.TestCase):
    def test_unknown_is_none(self):
        for raw in UNKNOWN:
            with self.subTest(raw=raw):
                self.assertIsNone(_parse_age_min_months_or_none(raw))

    def test_zero_months_is_zero(self):
        for raw in ("0ヶ月〜", "0歳〜", "0歳〜3歳", "0ヶ月〜12ヶ月"):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_age_min_months_or_none(raw), 0)

    def test_known_ages(self):
        for raw, months in (("6ヶ月〜", 6), ("3歳以上", 36), ("1歳半〜", 18), ("6歳", 72)):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_age_min_months_or_none(raw), months)

    def test_age_words_without_numbers(self):
        for raw, months in (
            ("小学生以上", 72),
            ("対象年齢の記載なし（小学生以上推奨）", 72),
            ("小学生以上（保護者同伴）", 72),
            ("幼児（受験期）", 36),
            ("幼児〜小学生", 36),
            ("中学生以上", 144),
            ("大人向け", 216),
            ("全年齢（大人用）", 216),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_age_min_months_or_none(raw), months)

    def test_numbers_win_over_words(self):
        self.assertEqual(_parse_age_min_months_or_none("3歳以上（大人の監督下で）"), 36)
        self.assertEqual(_parse_age_min_months_or_none("8歳〜大人向け"), 96)

    def test_legacy_parser_still_returns_zero_for_unknown(self):
        for raw in UNKNOWN:
            with self.subTest(raw=raw):
                self.assertEqual(_parse_age_min_months(raw), 0)
        self.assertEqual(_parse_age_min_months("3歳以上"), 36)


class FrontmatterAgeMinMonthsTests(unittest.TestCase):
    def _meta(self, target_age, name="テスト積み木"):
        data = {
            "title": "テスト",
            "product": {"asin": "B0GFVTPDBM", "target_age": target_age, "name": name},
            "date": "2026-10-08T10:00:00+09:00",
        }
        return _frontmatter_meta(
            data, "2026-10-08-B0GFVTPDBM", False, {}, pathlib.Path("B0GFVTPDBM.json"),
        )

    def test_unknown_age_is_not_written(self):
        for raw in ("", "対象年齢の記載なし", "全年齢"):
            with self.subTest(raw=raw):
                self.assertNotIn("age_min_months", self._meta(raw))

    def test_adult_is_written_as_216(self):
        self.assertEqual(self._meta("大人向け")["age_min_months"], 216)

    def test_seasonal_decoration_has_no_age(self):
        for name in ("五月人形 兜飾り クムキ 組", "吉利商事 鯉のぼり", "人と木 木製鏡餅 クムキ",
                     "飾りミニ羽子板 新助六", "クムキ 色 三宝雛飾り"):
            with self.subTest(name=name):
                self.assertNotIn("age_min_months", self._meta("0歳〜", name=name))

    def test_seasonal_play_toys_keep_age(self):
        # 福笑い・ひな祭りの工作キットは遊び道具なので年齢を持つ
        for name in ("CINECE 福笑い お正月遊びセット", "平和工業 MOCCO 木のホビーキット ひな祭り"):
            with self.subTest(name=name):
                self.assertEqual(self._meta("3歳〜", name=name)["age_min_months"], 36)

    def test_zero_months_is_written_as_zero(self):
        self.assertEqual(self._meta("0ヶ月〜")["age_min_months"], 0)

    def test_known_age_is_written(self):
        self.assertEqual(self._meta("3歳以上")["age_min_months"], 36)


class DevelopmentStageTests(unittest.TestCase):
    """対象年齢が不明な記事に「0〜2ヶ月の発達の目安」を出さない。"""

    STAGES = {"0m": {"age_label": "0〜2ヶ月"}, "3m": {"age_label": "3〜5ヶ月"}, "36m": {"age_label": "3歳"}}

    def test_unknown_age_has_no_stage(self):
        for raw in UNKNOWN:
            with self.subTest(raw=raw):
                self.assertIsNone(
                    _get_development_stage(_parse_age_min_months_or_none(raw), self.STAGES)
                )

    def test_zero_months_gets_newborn_stage(self):
        stage = _get_development_stage(_parse_age_min_months_or_none("0ヶ月〜"), self.STAGES)
        self.assertEqual(stage["age_label"], "0〜2ヶ月")

    def test_adult_has_no_stage(self):
        self.assertIsNone(_get_development_stage(_parse_age_min_months_or_none("大人向け"), self.STAGES))

    def test_known_age_gets_its_stage(self):
        stage = _get_development_stage(_parse_age_min_months_or_none("3歳以上"), self.STAGES)
        self.assertEqual(stage["age_label"], "3歳")


if __name__ == "__main__":
    unittest.main()
