"""#9186 — 対象年齢「不明」を age_min_months=0（0ヶ月〜）と区別する。

  - 数字の無い年齢表記 (空・「対象年齢の記載なし」「全年齢」「大人向け」) は不明。
    front matter に age_min_months を書かない (search.json では null になる)
  - 「0ヶ月〜」「0歳〜」のように 0 と書かれたものだけを 0 にする
  - _parse_age_min_months (不明も 0 を返す旧 API) の戻り値は変えない
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

UNKNOWN = (None, "", "  ", "対象年齢の記載なし", "記載なし", "全年齢", "大人向け", "指定なし")


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

    def test_legacy_parser_still_returns_zero_for_unknown(self):
        for raw in UNKNOWN:
            with self.subTest(raw=raw):
                self.assertEqual(_parse_age_min_months(raw), 0)
        self.assertEqual(_parse_age_min_months("3歳以上"), 36)


class FrontmatterAgeMinMonthsTests(unittest.TestCase):
    def _meta(self, target_age):
        data = {
            "title": "テスト",
            "product": {"asin": "B0GFVTPDBM", "target_age": target_age},
            "date": "2026-10-08T10:00:00+09:00",
        }
        return _frontmatter_meta(
            data, "2026-10-08-B0GFVTPDBM", False, {}, pathlib.Path("B0GFVTPDBM.json"),
        )

    def test_unknown_age_is_not_written(self):
        for raw in ("", "対象年齢の記載なし", "大人向け"):
            with self.subTest(raw=raw):
                self.assertNotIn("age_min_months", self._meta(raw))

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

    def test_known_age_gets_its_stage(self):
        stage = _get_development_stage(_parse_age_min_months_or_none("3歳以上"), self.STAGES)
        self.assertEqual(stage["age_label"], "3歳")


if __name__ == "__main__":
    unittest.main()
