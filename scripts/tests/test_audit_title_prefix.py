"""scripts/audit_title_prefix.py unit tests (#5083 項目3)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts.audit_title_prefix import (
    FALLBACK_SUFFIX,
    audit_article,
    fullwidth_width,
    intent_first_index,
    intent_hits,
    load_suffix,
    product_coverage,
    product_tokens,
    render_title,
    summarize,
    take_prefix,
)


def _tokenizer():
    from janome.tokenizer import Tokenizer
    return Tokenizer()


class WidthTest(unittest.TestCase):
    def test_fullwidth_counts_kana_as_one_and_latin_as_half(self):
        self.assertEqual(fullwidth_width("あいう"), 3.0)
        self.assertEqual(fullwidth_width("abcd"), 2.0)
        self.assertEqual(fullwidth_width("あa"), 1.5)
        self.assertEqual(fullwidth_width(""), 0.0)

    def test_take_prefix_chars_counts_characters(self):
        self.assertEqual(take_prefix("abcdefgh", 3, "chars"), "abc")
        self.assertEqual(take_prefix("あいうえお", 3, "chars"), "あいう")

    def test_take_prefix_fullwidth_fits_more_latin(self):
        # 全角 3.0 ぶん = ラテン 6 文字
        self.assertEqual(take_prefix("abcdefgh", 3, "fullwidth"), "abcdef")
        self.assertEqual(take_prefix("あいうえお", 3, "fullwidth"), "あいう")

    def test_take_prefix_does_not_overflow_on_boundary(self):
        # 残り 0.5 に全角 1.0 は入れない (切り上げて超過させない)
        self.assertEqual(take_prefix("aあ", 1.0, "fullwidth"), "a")

    def test_take_prefix_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            take_prefix("abc", 2, "pixels")


class RenderTitleTest(unittest.TestCase):
    def test_appends_suffix_like_head_html(self):
        self.assertEqual(render_title("商品の口コミ", "比較ナビ"), "商品の口コミ | 比較ナビ")

    def test_missing_parts_do_not_leave_dangling_separator(self):
        self.assertEqual(render_title("", "比較ナビ"), "比較ナビ")
        self.assertEqual(render_title("商品", ""), "商品")


class ProductMatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = _tokenizer()

    def test_tokens_drop_particles_and_single_kana(self):
        tokens = product_tokens("くもん出版 NEWたんぐらむ", self.tok)
        self.assertTrue(tokens)
        self.assertNotIn("の", tokens)
        self.assertNotIn(" ", tokens)

    def test_full_coverage_when_name_is_inside_prefix(self):
        cov, missing = product_coverage(
            "PLANTOYS 6404 ソリッドドラム",
            "プラントイ PLANTOYS 6404 ソリッドドラムは知育に効",
            self.tok,
        )
        self.assertEqual(cov, 1.0)
        self.assertEqual(missing, [])

    def test_partial_coverage_reports_missing_tokens(self):
        cov, missing = product_coverage(
            "GraviTrax ザ・コース",
            "ラベンスバーガー コンストラクショントイ GraviTrax ザ",
            self.tok,
        )
        self.assertLess(cov, 1.0)
        self.assertTrue(missing)

    def test_matching_is_case_and_width_insensitive(self):
        # normalize() の NFKC + lowercase が両側に効く
        cov, _ = product_coverage("BRIO レール", "ｂｒｉｏ レール の話", self.tok)
        self.assertEqual(cov, 1.0)

    def test_empty_name_is_zero_coverage_not_a_free_pass(self):
        cov, missing = product_coverage("", "なんでも入る前半", self.tok)
        self.assertEqual(cov, 0.0)
        self.assertEqual(missing, [])


class IntentTest(unittest.TestCase):
    def test_detects_keyword(self):
        self.assertIn("口コミ", intent_hits("商品の口コミを調べた"))

    def test_detects_age_expression(self):
        self.assertTrue(any("歳" in h for h in intent_hits("6歳からの積み木")))

    def test_returns_hits_in_appearance_order(self):
        hits = intent_hits("比較する前に口コミ")
        self.assertEqual(hits[0], "比較")

    def test_no_intent_returns_empty(self):
        self.assertEqual(intent_hits("ただの説明文"), [])
        self.assertIsNone(intent_first_index("ただの説明文"))

    def test_first_index_is_the_earliest_hit(self):
        self.assertEqual(intent_first_index("あ比較"), 1)


class AuditArticleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = _tokenizer()

    def _audit(self, article, limit=32):
        return audit_article(article, suffix="比較ナビ", limit=limit, tokenizer=self.tok)

    def test_both_true_when_name_and_intent_fit(self):
        row = self._audit({
            "slug": "2026-01-01-B000000001",
            "title": "つみきの口コミ・最安値を比較",
            "product": {"name": "つみき"},
        })
        self.assertTrue(row["modes"]["chars"]["both"])
        self.assertTrue(row["modes"]["chars"]["has_intent"])
        self.assertTrue(row["modes"]["chars"]["product_full"])

    def test_intent_pushed_past_the_limit_is_not_counted(self):
        row = self._audit({
            "slug": "2026-01-01-B000000002",
            "title": "あ" * 40 + "の口コミ",
            "product": {"name": "あ"},
        })
        self.assertFalse(row["modes"]["chars"]["has_intent"])
        self.assertFalse(row["modes"]["chars"]["both"])
        # タイトル全体でなら見つかるので、位置の分布には残る
        self.assertIsNotNone(row["intent_first_index_chars"])

    def test_missing_product_falls_back_to_name_full(self):
        row = self._audit({
            "title": "テスト商品の口コミ",
            "product": {"name_full": "テスト商品"},
        })
        self.assertFalse(row["product_name_missing"])
        self.assertTrue(row["modes"]["chars"]["product_full"])

    def test_missing_fields_do_not_crash(self):
        row = self._audit({})
        self.assertTrue(row["product_name_missing"])
        self.assertFalse(row["modes"]["chars"]["both"])
        self.assertFalse(row["modes"]["chars"]["product_full"])

    def test_product_is_not_a_dict(self):
        row = self._audit({"title": "口コミ", "product": "文字列"})
        self.assertTrue(row["product_name_missing"])

    def test_suffix_eats_into_the_prefix_for_short_titles(self):
        # 本文タイトルが短いと、前半にサフィックスが入り込む
        row = self._audit({"title": "口コミ", "product": {"name": "口コミ"}}, limit=32)
        self.assertIn("比較ナビ", row["modes"]["chars"]["prefix"])


class SummarizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = _tokenizer()

    def _rows(self):
        arts = [
            {"slug": "a", "title": "つみきの口コミ・最安値", "product": {"name": "つみき"}},
            {"slug": "b", "title": "あ" * 40 + "の口コミ", "product": {"name": "あ"}},
            {"slug": "c", "title": "ブロックの比較", "product": {"name": "ブロック"}},
        ]
        return [audit_article(a, suffix="比較ナビ", limit=32, tokenizer=self.tok)
                for a in arts]

    def test_ratios_are_over_the_whole_corpus(self):
        s = summarize(self._rows(), limit=32)
        self.assertEqual(s["total_articles"], 3)
        self.assertEqual(s["modes"]["chars"]["both"], 2)
        self.assertAlmostEqual(s["modes"]["chars"]["both_ratio"], 2 / 3, places=3)

    def test_samples_total_is_the_pre_truncation_count(self):
        rows = self._rows() * 20  # 20 件が both=False になる
        s = summarize(rows, limit=32, max_samples=2)
        self.assertEqual(s["modes"]["chars"]["samples_total"], 20)
        self.assertEqual(len(s["modes"]["chars"]["samples"]), 2)

    def test_product_name_over_limit_is_counted(self):
        rows = [audit_article(
            {"slug": "x", "title": "長い商品の口コミ", "product": {"name": "あ" * 40}},
            suffix="比較ナビ", limit=32, tokenizer=self.tok)]
        s = summarize(rows, limit=32)
        self.assertEqual(s["product_name_length"]["over_limit_chars"], 1)

    def test_empty_corpus_does_not_crash(self):
        s = summarize([], limit=32)
        self.assertEqual(s["total_articles"], 0)
        self.assertEqual(s["modes"], {})


class LoadSuffixTest(unittest.TestCase):
    def test_reads_product_title_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.toml"
            p.write_text('[params]\nproductTitleSuffix = "比較ナビ"\n', encoding="utf-8")
            self.assertEqual(load_suffix(p), "比較ナビ")

    def test_falls_back_when_key_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.toml"
            p.write_text("[params]\n", encoding="utf-8")
            self.assertEqual(load_suffix(p), FALLBACK_SUFFIX)

    def test_falls_back_when_file_is_missing(self):
        self.assertEqual(load_suffix(pathlib.Path("no/such/config.toml")),
                         FALLBACK_SUFFIX)

    def test_live_hugo_config_is_readable(self):
        # 実際の設定を読めなくなったら (キー改名等) 監査が黙って fallback に
        # 落ちるので、live の config に対しても 1 本張っておく。
        cfg = pathlib.Path("hugo/config.toml")
        if not cfg.exists():
            self.skipTest("hugo/config.toml not present")
        self.assertTrue(load_suffix(cfg))


class OutputShapeTest(unittest.TestCase):
    def test_summary_is_json_serializable(self):
        tok = _tokenizer()
        rows = [audit_article({"slug": "a", "title": "つみきの口コミ",
                               "product": {"name": "つみき"}},
                              suffix="比較ナビ", limit=32, tokenizer=tok)]
        json.dumps(summarize(rows, limit=32), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
