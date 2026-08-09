"""Unit tests — 読者向け本文から生の ASIN コードを落とす (2026-08-09)。

narrative.how_to_choose (#3203 v7 の「選び分け」節) に Amazon 内部コードがそのまま
出ていた。実測で公開 1913 ページ中 107 ページ・144 箇所。quality_gate の
check_how_to_choose は competitors.json との照合 (封じ込め) であって言及自体は許可
しているため、素通りしていた。

期待値はすべて実データから採取した文字列で書いてある。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _strip_inline_asin_codes  # type: ignore[import-not-found]  # noqa: E402


class StripInlineAsinTest(unittest.TestCase):
    def test_full_width_paren_is_removed_whole(self):
        s = "初めてのお試しなら30ピースの『ベーシックプラスセット』（B0BD3WZG32）もあります。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "初めてのお試しなら30ピースの『ベーシックプラスセット』もあります。",
        )

    def test_half_width_paren_is_removed_whole(self):
        s = "アガツマの「ニャンだきみは! ?」(B0CTJDMTR8)などは音や動きで楽しませる。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "アガツマの「ニャンだきみは! ?」などは音や動きで楽しませる。",
        )

    def test_asin_label_form_is_removed(self):
        s = "競合の『えいご タブレット』(ASIN: B00RZ4LL3Y) は英語学習向けです。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "競合の『えいご タブレット』 は英語学習向けです。",
        )

    def test_paren_with_only_filler_is_removed(self):
        s = "同ブランドの「Smart Phonics」（B0CVKMN1NZ など）は読み書きの練習向け。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "同ブランドの「Smart Phonics」は読み書きの練習向け。",
        )

    def test_paren_with_real_content_keeps_the_paren(self):
        """括弧に商品名などの実質情報があるときはコードだけ抜く。"""
        s = "手先の感覚だけを頼りにするパズル（平和工業 立体4目並べ:B000LVJSLK など）は直感的。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "手先の感覚だけを頼りにするパズル（平和工業 立体4目並べ など）は直感的。",
        )

    def test_code_before_closing_quote_is_removed(self):
        s = "上級版の「迷宮ボール B0G4R9XVY6」が向いています。"
        self.assertEqual(
            _strip_inline_asin_codes(s),
            "上級版の「迷宮ボール」が向いています。",
        )

    def test_bare_in_sentence_code_is_left_alone(self):
        """地の文に埋め込まれた裸のコードは消すと日本語が壊れるので触らない。

        これは生成側 (プロンプト) で禁じるべき事象で、レンダリング時の後始末では
        安全に直せない。実データでは 148 箇所中 2 箇所。
        """
        s = "同じような3種セットの B0H1HK771S は価格が1599円です。"
        self.assertEqual(_strip_inline_asin_codes(s), s)

    def test_list_values_are_handled(self):
        v = ["本品は62ピースです。", "『ベーシックプラスセット』（B0BD3WZG32）もあります。"]
        self.assertEqual(
            _strip_inline_asin_codes(v),
            ["本品は62ピースです。", "『ベーシックプラスセット』もあります。"],
        )

    def test_text_without_asin_is_untouched(self):
        s = "ピース数とパーツの種類のバランスで選びましょう。"
        self.assertEqual(_strip_inline_asin_codes(s), s)

    def test_url_is_not_damaged(self):
        """narrative に URL が入っていてもリンクを壊さない。"""
        s = "詳細は https://www.amazon.co.jp/dp/B0BD3WZG32/ を参照してください。"
        self.assertEqual(_strip_inline_asin_codes(s), s)

    def test_non_string_values_pass_through(self):
        self.assertIsNone(_strip_inline_asin_codes(None))
        self.assertEqual(_strip_inline_asin_codes(42), 42)


if __name__ == "__main__":
    unittest.main()
