"""_age_fit_score が小数の年齢 (1.5歳) を正しく読むことの検証。

以前は整数だけの正規表現が "1.5歳" の "5歳" だけを拾い、「5歳以上」と表示していた。
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from score_calculator import _age_fit_score, _price_value_score  # noqa: E402


def _age(text):
    return _age_fit_score({"target_age": text})


class AgeFitDecimalTest(unittest.TestCase):
    def test_decimal_lower_bound_only(self):
        for text in ("1.5歳以上", "1.5歳から", "1.5歳〜"):
            pts, reason, rng = _age(text)
            self.assertEqual(pts, 10, text)
            self.assertIn("1.5歳以上の対象年齢が明示", reason)
            self.assertEqual(rng, (1, 6), text)

    def test_decimal_lower_bound_kanji_sai(self):
        pts, reason, rng = _age("1.5才以上")
        self.assertIn("1.5才以上の対象年齢が明示", reason)
        self.assertEqual(rng, (1, 6))

    def test_decimal_range(self):
        pts, reason, rng = _age("1.5歳〜3歳")
        self.assertEqual(pts, 10)
        self.assertIn("対象年齢は1.5歳〜3歳に", reason)
        self.assertEqual(rng, (1, 3))

    def test_decimal_range_upper_bound(self):
        pts, reason, rng = _age("3歳〜4.5歳")
        self.assertIn("3歳〜4.5歳", reason)
        self.assertEqual(rng, (3, 5))

    def test_decimal_range_wide(self):
        pts, reason, rng = _age("1.5才〜7歳")
        self.assertIn("1.5才〜7歳と幅広く", reason)
        self.assertEqual(rng, (1, 7))

    def test_decimal_range_not_inverted(self):
        # 旧実装は 1.5歳〜3歳 を 下限5・上限3 と読んでいた
        _, _, rng = _age("1.5歳〜3歳")
        self.assertLessEqual(rng[0], rng[1])

    def test_integer_unchanged(self):
        self.assertEqual(_age("6歳以上")[2], (6, 11))
        self.assertIn("6歳以上の対象年齢が明示", _age("6歳以上")[1])
        self.assertEqual(_age("3歳〜7歳")[2], (3, 7))
        self.assertEqual(_age("3才〜5才")[2], (3, 5))

    def test_months_unchanged(self):
        self.assertEqual(_age("6ヶ月〜2歳")[2], (1, 2))
        self.assertEqual(_age("12ヶ月から")[2], (1, 6))
        self.assertEqual(_age("18ヶ月以上")[2], (2, 7))

    def test_textual_and_empty_unchanged(self):
        self.assertEqual(_age("幼児向け")[0], 6)
        self.assertIsNone(_age("幼児向け")[2])
        self.assertEqual(_age("")[0], 4)

    def test_age_range_feeds_price_value_years(self):
        # 1.5歳〜3歳 → 3 年。旧実装の (5, 3) だと max(1, -1) = 1 年になっていた
        _, _, rng = _age("1.5歳〜3歳")
        pts, reason = _price_value_score({"price": 3000}, rng)
        self.assertIn("¥1,000", reason)

class AgeFitHalfAndAboutTest(unittest.TestCase):
    def test_half_lower_bound(self):
        for text in ("1歳半以上", "1歳半〜", "1歳半から", "1才半以上"):
            pts, reason, rng = _age(text)
            self.assertEqual(pts, 10, text)
            self.assertIn("1" + text[1] + "半以上の対象年齢が明示", reason)
            self.assertEqual(rng, (1, 6), text)

    def test_half_range(self):
        pts, reason, rng = _age("1歳半〜3歳")
        self.assertEqual(pts, 10)
        self.assertIn("対象年齢は1歳半〜3歳に", reason)
        self.assertEqual(rng, (1, 3))
        self.assertEqual(_age("1歳半〜4歳")[2], (1, 4))

    def test_half_upper_bound(self):
        self.assertEqual(_age("2歳〜3歳半")[2], (2, 4))

    def test_about_age(self):
        for text in ("3歳頃〜", "3歳頃から", "3歳頃以上"):
            pts, reason, rng = _age(text)
            self.assertEqual(pts, 10, text)
            self.assertEqual(rng, (3, 8), text)
        self.assertEqual(_age("1歳頃〜")[2], (1, 6))
        self.assertIn("3歳頃からの対象年齢が明示", _age("3歳頃〜")[1])
        self.assertNotIn("頃以上", _age("3歳頃〜")[1])

    def test_fullwidth_tilde(self):
        # 全角チルダ「～」(U+FF5E) も区切りとして読む
        self.assertEqual(_age("1.5歳～")[:1] + _age("1.5歳～")[2:], (10, (1, 6)))
        self.assertEqual(_age("2歳～4歳")[2], (2, 4))

    def test_about_range(self):
        self.assertEqual(_age("3歳頃〜5歳頃")[2], (3, 5))

    def test_about_without_follow_is_not_read(self):
        self.assertEqual(_age("3歳頃")[0], 6)


if __name__ == "__main__":
    unittest.main()
