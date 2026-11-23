"""#9217: FAQ が「公式の対象年齢の記載は無い」と答えている記事の年齢は目安扱い。"""
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

import age_semantics  # noqa: E402
import score_calculator  # noqa: E402
from brand_normalizer import NormalizedBrand  # noqa: E402

NO_AGE_ANSWERS = [
    "明確な対象年齢の記載はありませんが、2歳頃から遊べる設計です。",
    "公式の対象年齢は明記されていませんが、細かな操作があるため3歳頃からが目安です。",
    "対象年齢は特に明記されていませんが、簡単な計算ができる年齢から楽しめます。",
    "明確な対象年齢の下限は記載されていませんが、一般的には3歳頃からです。",
    "公式の推奨年齢に明確な記載はありませんが、音楽に合わせて楽しめます。",
    "対象年齢は明確に設定されていませんが、小さなパーツが含まれます。",
    "対象年齢の指定はありませんが、3歳頃からが目安です。",
    "本品は公式に年齢指定が明記されていませんが、3歳以上に適しています。",
    "対象年齢は販売ページの仕様上、明確な記載がありませんが、3歳以上が目安です。",
    "対象年齢の記載はない。",
]


def _article(answer: str, asin: str = "B000TEST01", target_age: str = "2歳〜") -> dict:
    return {
        "product": {"asin": asin, "target_age": target_age},
        "faq": [{"question": "何歳から遊べますか？", "answer": answer}],
    }


class FaqSaysNoOfficialAgeTest(unittest.TestCase):
    def test_detects_no_official_age_phrasings(self):
        for a in NO_AGE_ANSWERS:
            with self.subTest(a=a):
                self.assertTrue(age_semantics.faq_says_no_official_age(_article(a)))

    def test_stated_age_is_not_flagged(self):
        for a in ("対象年齢は3歳以上です。小さな部品に注意してください。",
                  "メーカーの対象年齢は6歳以上と明記されています。",
                  "対象年齢は3歳以上と明記されており、不安はありません。",
                  "対象年齢の記載があり、問題はありません。",
                  "対象年齢は3歳以上と明記されていますが、上限年齢の記載はありません。",
                  "年齢別の遊び方の記載はありませんが、対象年齢は3歳以上です。"):
            with self.subTest(a=a):
                self.assertFalse(age_semantics.faq_says_no_official_age(_article(a)))

    def test_faq_extended_is_read(self):
        raw = {"product": {"asin": "B000TEST01"},
               "faq_extended": [{"question": "q", "answer": NO_AGE_ANSWERS[0]}]}
        self.assertTrue(age_semantics.faq_says_no_official_age(raw))

    def test_override_with_source_wins(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "age_overrides.json"
            p.write_text(json.dumps({"overrides": {"B000TEST01": {
                "age_min_months": 36, "source": "https://example.com/"}}}), encoding="utf-8")
            raw = _article(NO_AGE_ANSWERS[0])
            self.assertFalse(age_semantics.age_is_estimate(raw, p))
            self.assertTrue(age_semantics.age_is_estimate(_article(NO_AGE_ANSWERS[0], "B000OTHER1"), p))


class AgeFitScoreEstimateTest(unittest.TestCase):
    def _score(self, article):
        brand = NormalizedBrand(canonical="", tier="D", region="unknown")
        r = score_calculator.calculate(article, brand)
        return r

    def test_estimate_is_not_full_marks(self):
        r = self._score(_article(NO_AGE_ANSWERS[0]))
        self.assertEqual(r.breakdown["age_fit"], 6)
        self.assertTrue(any("目安" in x for x in r.rationale))

    def test_stated_age_keeps_full_marks(self):
        r = self._score(_article("対象年齢は3歳以上です。"))
        self.assertEqual(r.breakdown["age_fit"], 10)


if __name__ == "__main__":
    unittest.main()
