"""build_post._matched_passes_quality の閾値ガードのユニットテスト。

Issue #1072 Phase 3-B (2026-05-31): jan_unknown 58 ASIN を救済するため
price band を [0.5, 2.0] → [0.4, 2.5], coverage を 0.7 → 0.5 に緩和した。
dry-run (scripts/analyze_threshold_relaxation.py) の救済候補 25 件のうち
代表ケースをテストで pin して、将来の閾値 drift を検知する。
"""

from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_post  # noqa: E402


class MarketQualityGateConstantsTest(unittest.TestCase):
    """Pin the relaxed preset (#1072 Phase 3-B)."""

    def test_price_band_low(self):
        self.assertEqual(build_post._MARKET_PRICE_BAND_LOW, 0.4)

    def test_price_band_high(self):
        self.assertEqual(build_post._MARKET_PRICE_BAND_HIGH, 2.5)

    def test_coverage_ratio(self):
        self.assertEqual(build_post._MARKET_COVERAGE_RATIO, 0.5)


class MatchedPassesQualityTest(unittest.TestCase):
    """Real-world cases from scratch/threshold_relaxation.md."""

    def _check(self, *, title: str, price: int, kw: str, amazon_price: int) -> bool:
        return build_post._matched_passes_quality(
            {"title": title, "price": price, "search_keyword": kw},
            amazon_price,
        )

    # === relax_both で救済される代表ケース (dry-run で確認済) ===

    def test_coverage_050_rescued(self):
        """B097GY576F: WYSWYG 240ピース、cov=0.50 (旧 0.7 で fail)。"""
        self.assertTrue(self._check(
            title="【送料無料】WYSWYG 240ピース創造大きいブロックセット レゴ デュプロ互換",
            price=4899,
            kw="WYSWYG 240ピース創造大きいブロックセット 積み木 -デュプロ/アンパン",
            amazon_price=3041,
        ))

    def test_coverage_067_rescued(self):
        """B09BQMCSFL: ハズブロ ナーフ RD-6、cov=0.67 (旧 0.7 で fail)。"""
        self.assertTrue(self._check(
            title="ハズブロ ナーフ エリート 2.0 コマンダー RD−6",
            price=1775,
            kw="ハズブロ ナーフ RD-6",
            amazon_price=2302,
        ))

    def test_price_ratio_210_rescued(self):
        """B0875FV2BQ: ロンビー中古プレミア、ratio=2.10 (旧 2.0 で fail)。"""
        self.assertTrue(self._check(
            title="【中古】トイオブザイヤー2025受賞 ロンビー (Lon-Bi) 自分でつくる屋内遊具",
            price=6613,
            kw="トイオブザイヤー2025受賞 ロンビー 自分でつくる屋内遊具 室内遊び",
            amazon_price=3149,
        ))

    # === aggressive only / 旧来通り fail させ続けたい境界 ===

    def test_price_ratio_275_still_rejected(self):
        """B0GFVV4YG9: ratio=2.75 は relax_aggressive のみで pass、本実装では fail のまま。"""
        self.assertFalse(self._check(
            title="Mamimami Home アクティビティキューブ 木製 型はめ モンテッソーリ",
            price=3962,
            kw="Mamimami Home ミニカー 木製",
            amazon_price=1439,
        ))

    def test_price_below_band_rejected(self):
        """0.4x 未満は除外。"""
        self.assertFalse(self._check(
            title="ハズブロ ナーフ エリート 2.0 コマンダー",
            price=399,
            kw="ハズブロ ナーフ コマンダー",
            amazon_price=2302,
        ))

    def test_no_meaningful_tokens_passes(self):
        """汎用語のみの search_keyword は cross-search 側 median band 選出を尊重して pass。"""
        self.assertTrue(self._check(
            title="知育玩具 木製パズル",
            price=2000,
            kw="おもちゃ 知育玩具 木製",  # 全部 _MARKET_GENERIC_TOKENS
            amazon_price=1800,
        ))

    # === 型番ガード (B0CDTQWRN1 reseller false-positive 対策) ===

    def test_rejects_model_number_mismatch(self):
        """B0CDTQWRN1: kw 'E3209' (レジカウンター) vs title はファーマーズマーケット
        (型番 E3209 無し) → 別 SKU の誤マッチとして弾く。"""
        self.assertFalse(self._check(
            title="HAPE ハペ おままごと 3歳 4歳 お店屋さんごっこ ファーマーズマーケット 食べ物 木製 セット バナナ スイカ",
            price=10679,
            kw="Hape おままごとレジカウンター E3209",
            amazon_price=15950,
        ))

    def test_accepts_model_number_present(self):
        """型番が title に存在すれば pass (E0328 が title に在る正規マッチ)。"""
        self.assertTrue(self._check(
            title="おもちゃ Hape ビーズコインドロップス E0328 木製",
            price=3000,
            kw="Hape ビーズコインドロップス E0328",
            amazon_price=3000,
        ))

    def test_single_digit_code_not_treated_as_model(self):
        """RD-6 等の 1 桁コードは型番扱いせず既存 coverage ロジックに委ねる
        (>=2 数字要件で false-trip を避ける regression guard)。"""
        self.assertTrue(self._check(
            title="ハズブロ ナーフ エリート 2.0 コマンダー RD−6",
            price=1775,
            kw="ハズブロ ナーフ RD-6",
            amazon_price=2302,
        ))

    def test_model_number_fullwidth_hyphen_accepted(self):
        """型番 (>=2 数字) の全角ハイフン揺れを吸収して一致判定する。"""
        self.assertTrue(self._check(
            title="トミカ プレミアム TF−12 日産 スカイライン GT-R",
            price=900,
            kw="トミカ プレミアム TF-12 スカイライン",
            amazon_price=880,
        ))


class NonBrandDescriptorGateTest(unittest.TestCase):
    """Issue #1140: brand head だけで通過する FP を非ブランド descriptor 一致で弾く。"""

    def _check(self, *, title: str, price: int, kw: str, amazon_price: int) -> bool:
        return build_post._matched_passes_quality(
            {"title": title, "price": price, "search_keyword": kw},
            amazon_price,
        )

    # === FP として弾くべき Mamimami Home 系 (Issue #1140 リスト) ===

    def test_rejects_mamimami_tissue_vs_pazzle(self):
        """B0DJSB2MPD: kw 'ティッシュ 赤ちゃん' vs 型はめパズル → brand head のみ一致で FP。"""
        self.assertFalse(self._check(
            title="Mamimami Home 型はめ パズル 形合わせ はめ込み 木製 パズルボックス モンテッソーリ",
            price=3944,
            kw="Mamimami Home ティッシュ 赤ちゃん",
            amazon_price=3500,
        ))

    def test_rejects_mamimami_blocks_vs_cube(self):
        """B0DY7C8SKY: kw '積み木 車' vs アクティビティキューブ → 同上 FP。"""
        self.assertFalse(self._check(
            title="Mamimami Home アクティビティキューブ 木製 型はめ モンテッソーリ パズルボックス",
            price=3962,
            kw="Mamimami Home 積み木 車",
            amazon_price=3800,
        ))

    def test_rejects_mamimami_gakki_vs_box(self):
        """B0GFWDGHT9: kw '楽器 木製' vs パズルボックス → FP。"""
        self.assertFalse(self._check(
            title="Mamimami Home パズルボックス 木製 型はめ モンテッソーリ 知育玩具",
            price=3500,
            kw="Mamimami Home 楽器 木製",
            amazon_price=3200,
        ))

    # === 通すべき正常ケース (regression guard) ===

    def test_accepts_when_descriptor_substring_hits(self):
        """正規 Mamimami ティッシュ商品なら descriptor が部分一致して pass。"""
        self.assertTrue(self._check(
            title="Mamimami Home ティッシュボックス 赤ちゃん 引っ張り出す 木製 おもちゃ",
            price=3500,
            kw="Mamimami Home ティッシュ 赤ちゃん",
            amazon_price=3500,
        ))

    def test_accepts_when_no_common_prefix(self):
        """共通 prefix が無ければ既存 coverage gate に委ねる (新ロジックは spurious fail しない)。"""
        self.assertTrue(self._check(
            title="おもちゃ Hape ビーズコインドロップス E0328 木製",
            price=3000,
            kw="Hape ビーズコインドロップス E0328",
            amazon_price=3000,
        ))

    def test_accepts_when_descriptor_empty(self):
        """kw が brand head しか無い場合は descriptor 要件を課さない。"""
        self.assertTrue(self._check(
            title="タカラトミー リカちゃん 公式 グッズ",
            price=2500,
            kw="タカラトミー リカちゃん",  # 2 tokens 全部 prefix
            amazon_price=2500,
        ))

    def test_accepts_suffix_variation_via_bidirectional(self):
        """suffix 揺れ (タッチペン vs タッチペン付) を双方向 substring で吸収する。"""
        self.assertTrue(self._check(
            title="BabyBus はじめてのちいくずかん 日本語 英語 中国語 タッチペン 音楽再生 知育玩具",
            price=8000,
            kw="BabyBus はじめてのちいくずかん タッチペン付",
            amazon_price=8000,
        ))

    def test_accepts_html_entity_decoded(self):
        """title の &amp; を & に decode してから比較する (パイロット ペン2本&スタンプセット)。"""
        self.assertTrue(self._check(
            title="パイロット スイスイおえかき カラフルシート ペン2本&amp;スタンプセット 知育玩具",
            price=3000,
            kw="パイロット スイスイおえかき カラフルシート ペン2本&スタンプセット",
            amazon_price=3000,
        ))

    def test_accepts_middle_dot_variation(self):
        """半角中黒 ･ と全角中黒 ・ を同一視 (トイ・ストーリー)。"""
        self.assertTrue(self._check(
            title="タカラトミー トミカプレミアム トイ・ストーリー ピザ・プラネット トラック",
            price=2000,
            kw="タカラトミー トミカプレミアム トイ･ストーリー ピザ･プラネット",
            amazon_price=2000,
        ))


if __name__ == "__main__":
    unittest.main()
