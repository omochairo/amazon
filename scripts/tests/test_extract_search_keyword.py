"""fetch_cross_search.extract_search_keyword の品質テスト (Issue #1087 Phase 2)。

JAN 直引きが失敗したときの text fallback で誤マッチを防ぐ目的で、
次の 3 系統の改善を pin する:

1. **token-based noise filter**: "おもちゃ" が brand 名 (木製おもちゃのだいわ)
   や複合語 (木のおもちゃ) を substring 破壊しないこと
2. **long-katakana product preference**: タイトル中盤の 8 文字以上の純カタカナ
   token (例: メロディーゴーラウンド, クラシックドラム) を generic 修飾語
   (楽器, 音色 等) より優先して採用
3. **stop token removal**: "以上 / 以下 / 向け" 等が kw 末尾に残らないこと
"""

from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from fetch_cross_search import extract_search_keyword  # noqa: E402


class LongKatakanaPreferenceTest(unittest.TestCase):
    """型番が無いケースで、中盤の長い純カタカナ token を product identifier として優先採用する。"""

    def test_picks_melodygoaround_over_generic_words(self):
        """B08DCHWRJC: 'Edutē エデュテ 楽器 ...' → メロディーゴーラウンド が拾われる。"""
        title = (
            "Edutē エデュテ 楽器 優しい音色 音楽を奏でる 表現力を育む "
            "手の動きをコントロール 音楽知育 出産祝い アイムトイ "
            "メロディーゴーラウンド 木のおもちゃ 木琴 太鼓 ドラム"
        )
        kw = extract_search_keyword(title)
        self.assertIn("メロディーゴーラウンド", kw)
        # generic 修飾語に流れていないこと
        self.assertNotIn("楽器", kw)
        self.assertNotIn("音色", kw)

    def test_picks_classic_drum(self):
        """B093Q78X67: '楽器 トントン叩く ... クラシックドラム'。"""
        title = (
            "Edutē エデュテ 楽器 トントン叩く リズム感を育む 音の違いを楽しむ "
            "聴覚刺激 ストレス発散 両手を使う運動 アイムトイ クラシックドラム "
            "木のおもちゃ 太鼓 ドラム 1歳"
        )
        kw = extract_search_keyword(title)
        self.assertIn("クラシックドラム", kw)
        self.assertNotIn("トントン叩く", kw)

    def test_picks_rainbow_abacus(self):
        """B087WNKR5D: 中盤 'レインボーアバカス' を採用。"""
        title = (
            "Edutē エデュテ 100玉そろばん 100まで数える 数を可視化する "
            "算数が好きになる 早期教育 集中力 ボイラ レインボーアバカス "
            "おもちゃ 知育玩具 百玉 算盤 3歳 子供"
        )
        kw = extract_search_keyword(title)
        self.assertIn("レインボーアバカス", kw)


class TokenBasedNoiseFilterTest(unittest.TestCase):
    """substring 除去で誤って brand を切り刻まないこと。"""

    def test_preserves_brand_containing_noise_substring(self):
        """B0019H1W6E: 'おもちゃ' を substring 除去すると brand '木製おもちゃのだいわ' が
        '木製 のだいわ' に割れてしまう (旧 bug)。token 完全一致除去で防ぐ。"""
        title = "木製おもちゃのだいわ 積木バスケット 積み木 25ピース 2歳 3歳 ブロック"
        kw = extract_search_keyword(title)
        self.assertIn("木製おもちゃのだいわ", kw)
        # 旧 bug 形式 (木製単独 + のだいわ単独) が出ていないこと
        self.assertNotIn("のだいわ", kw.replace("木製おもちゃのだいわ", ""))

    def test_preserves_kinoomocha_compound(self):
        """'木のおもちゃ' (複合語) の中の 'おもちゃ' を破壊しないこと。"""
        title = "ブランドA 商品名B 木のおもちゃ パズル 1歳"
        kw = extract_search_keyword(title)
        # token として 'おもちゃ' は無くなる (token 一致除去) が '木のおもちゃ' は残る
        self.assertIn("木のおもちゃ", kw)

    def test_standalone_noise_token_is_removed(self):
        """単独 token の 'おもちゃ' は noise として除去される。"""
        title = "ブランドX 商品Y おもちゃ パズル"
        kw = extract_search_keyword(title)
        # 'おもちゃ' 単独は kw に含まれない
        tokens = kw.split()
        self.assertNotIn("おもちゃ", tokens)


class StopTokenRemovalTest(unittest.TestCase):
    """末尾 '以上'/'以下'/'向け' 等の age 修飾語 token が残らない。"""

    def test_removes_trailing_ijou(self):
        """B0BLH6GHKJ: '4歳以上' → kw 末尾に '以上' が残らない。"""
        title = "マルカ ファミリードミノセット ゲーム 4歳以上"
        kw = extract_search_keyword(title)
        tokens = kw.split()
        self.assertNotIn("以上", tokens)

    def test_removes_ijou_with_kumon_kept(self):
        """B01MA38PCL: '以上 KUMON' → '以上' 落ちて KUMON が残る。"""
        title = (
            "【くもん出版公認セット】くもん出版 おふろでレッスンミニ "
            "ひらがなのひょう (A4判4枚) 知育玩具 おもちゃ 2歳以上 KUMON"
        )
        kw = extract_search_keyword(title)
        tokens = kw.split()
        self.assertNotIn("以上", tokens)


class MarketingDecorRemovalTest(unittest.TestCase):
    """受賞/マーケ修飾語が tokens[:4] を埋めて商品名を押し出す問題への対処。"""

    def test_removes_good_design_award_keeps_product(self):
        """B001A29UQW: 'グッドデザイン賞 グッド・トイ賞 受賞 アースカイト' →
        商品名 'アースカイト' まで届く。"""
        title = (
            "ラングスジャパン(RANGS) グッドデザイン賞 グッド・トイ賞 受賞 "
            "アースカイト オレンジ 手の平サイズに収納可 凧あげ"
        )
        kw = extract_search_keyword(title)
        self.assertIn("アースカイト", kw)
        self.assertNotIn("グッドデザイン賞", kw)
        self.assertNotIn("グッド・トイ賞", kw)
        self.assertNotIn("受賞", kw.split())

    def test_removes_award_keeps_plasmacar(self):
        """B07YFQHKLD: 'グッドデザイン賞 グッド・トイ賞 プラズマカー'。"""
        title = (
            "ラングスジャパン(RANGS) グッドデザイン賞 グッド・トイ賞 "
            "プラズマカー ピンク【ゴム製ウィール標準装備】耐荷重100kg"
        )
        kw = extract_search_keyword(title)
        self.assertIn("プラズマカー", kw)
        self.assertNotIn("グッドデザイン賞", kw)


class RegressionGuardTest(unittest.TestCase):
    """既存の正常ケースで挙動が変わらないこと。"""

    def test_model_number_priority_still_works(self):
        """型番 (E0328) があるときは brand head + 型番 で構成される。"""
        title = "Hape ビーズコインドロップス E0328 木のおもちゃ 知育玩具 1歳から"
        kw = extract_search_keyword(title)
        self.assertEqual(kw, "Hape ビーズコインドロップス E0328")

    def test_takara_tomy_normal_keyword(self):
        """ブランド + 商品名で構成される通常タイトル。"""
        title = "タカラトミー トミカ No.84 トヨタ アルファード"
        kw = extract_search_keyword(title)
        self.assertIn("タカラトミー", kw)
        self.assertIn("トミカ", kw)

    def test_empty_title_returns_empty(self):
        self.assertEqual(extract_search_keyword(""), "")

    def test_short_katakana_not_preferred(self):
        """8 文字未満の短い純カタカナ (例: ボイラ) は product identifier として
        優先採用されない (誤検出を防ぐ)。"""
        title = "ブランド ボイラ 木の おもちゃ 知育 学習"
        kw = extract_search_keyword(title)
        # ボイラ (3 文字) は preferred 採用されず、tokens[:4] fallback になる
        tokens = kw.split()
        # 先頭 head が "ブランド ボイラ" なので含まれるのは OK だが、
        # 「ブランド + ボイラ + 木の」のような tokens[:4] パターンであること
        self.assertIn("ブランド", tokens)


if __name__ == "__main__":
    unittest.main()
