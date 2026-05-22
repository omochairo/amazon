"""competitor_ranking.py の純粋関数を stdlib unittest で検証する。

実行: `python -m unittest scripts.tests.test_competitor_ranking` を amazon-clone 直下から、
または `python scripts/tests/test_competitor_ranking.py`。

PA-API はモックせず触らない (この module は pure function のみ)。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # scripts/ を import path に追加

from competitor_ranking import (  # noqa: E402
    build_search_keyword, title_similarity, price_proximity,
    dedupe_candidates, rank_candidates, ensure_internal_link_floor,
)


class BuildSearchKeywordTests(unittest.TestCase):

    def test_strips_bracketed_brand_and_filler(self):
        title = "アガツマ(AGATSUMA) おうたもあいうえおも! アンパンマン はじめてのキッズタブレット"
        kw = build_search_keyword(title, max_tokens=3)
        # ブランド括弧 (AGATSUMA) は落ちる
        self.assertNotIn("(", kw)
        self.assertNotIn("AGATSUMA", kw)
        # 主要要素のどれかは残るはず
        self.assertTrue(any(t in kw for t in ["アンパンマン", "おうた", "あいうえお", "タブレット", "アガツマ"]))
        # 単独 filler ("玩具" "知育玩具" "プレゼント" など) はトークンとして残らない
        kw_tokens = kw.split()
        for f in ["玩具", "知育玩具", "プレゼント", "誕生日"]:
            self.assertNotIn(f, kw_tokens)

    def test_short_keywords_have_no_more_than_max_tokens(self):
        title = "Joyreal モンテッソーリ ビジーボード 知育玩具 3歳 誕生日プレゼント 男の子"
        kw = build_search_keyword(title, max_tokens=3)
        self.assertLessEqual(len(kw.split()), 3)
        # filler は確実に落ちる
        for filler in ["3歳", "誕生日プレゼント", "男の子", "知育玩具"]:
            self.assertNotIn(filler, kw.split())

    def test_compound_words_extracted_correctly(self):
        # 漢字＋ひらがな＋漢字の複合語が1語で正しく抽出されることを検証
        title1 = "脳トレ積み木パズル 木製ブロック"
        kw1 = build_search_keyword(title1, max_tokens=3)
        self.assertIn("脳トレ積み木パズル", kw1.split())

        title2 = "エド・インター あそび方ガイド付き積み木"
        kw2 = build_search_keyword(title2, max_tokens=3)
        self.assertIn("あそび方ガイド付き積み木", kw2.split())

    def test_filler_removal_with_joshi(self):
        # 助詞を伴う filler 語 (「おもちゃの」「プレゼントに」等) が除去されることを検証
        title = "子供へのおもちゃのプレゼントに最適！レゴブロック"
        kw = build_search_keyword(title, max_tokens=3)
        # 「レゴブロック」が主要キーワードとして残るべき
        self.assertIn("レゴブロック", kw.split())
        # filler（おもちゃの、プレゼントに、等）は除去されているべき
        kw_tokens = kw.split()
        for f in ["おもちゃの", "プレゼントに", "おもちゃ", "プレゼント", "子供への", "子供"]:
            self.assertNotIn(f, kw_tokens)

    def test_exception_dictionary_preserves_hiragana_words(self):
        """例外辞書 (_EXCEPTION_WORDS) に含まれるひらがな固有名詞が、助詞切り離しで破壊されないことを検証。

        【既知の副作用ケース (Known Issues - 計7件 5単語)】
        V11→V12の助詞切り離しロジック移行時に、以下のひらがな固有名詞で語尾が助詞と誤判定される副作用が発生した。
        これらは _EXCEPTION_WORDS に登録することで回避されている。
        1. のりもの (のりも + の) -> 2件
        2. おままごと (おままご + と) -> 2件
        3. いつまで (いつま + で) -> 1件
        4. おかいもの (おかいも + の) -> 1件
        5. なに (な + に) -> 1件
        """
        # 例外辞書対象語のテスト
        title = "のりもの おままごと いつまで おかいもの なに"
        kw = build_search_keyword(title, max_tokens=5)
        tokens = kw.split()
        self.assertIn("のりもの", tokens)
        self.assertIn("おままごと", tokens)
        self.assertIn("いつまで", tokens)
        self.assertIn("おかいもの", tokens)
        self.assertIn("なに", tokens)


class TitleSimilarityTests(unittest.TestCase):

    def test_identical_titles_score_one(self):
        t = "レゴ シティ ポリスカー"
        self.assertAlmostEqual(title_similarity(t, t), 1.0)

    def test_unrelated_titles_score_low(self):
        a = "レゴ シティ ポリスカー ブロック"
        b = "ぬいぐるみ うさぎ もちもち かわいい"
        self.assertLess(title_similarity(a, b), 0.15)

    def test_same_brand_different_product_scores_mid(self):
        # 同ブランドのみ共通 (商品種が違う) は中-低スコア。
        # 単一ブランドトークン共有では 4-5 文字長の char 2-gram 寄与しか
        # 期待できないので 0.03-0.30 程度。「無関係 (0.0)」より高ければ良い。
        a = "レゴ シティ ポリスカー"
        b = "レゴ スター・ウォーズ Xウィング"
        score = title_similarity(a, b)
        unrelated = title_similarity(a, "ぬいぐるみ うさぎ もちもち")
        self.assertGreater(score, unrelated)
        self.assertLess(score, 0.7)


class PriceProximityTests(unittest.TestCase):

    def test_equal_prices_score_one(self):
        self.assertAlmostEqual(price_proximity(1000, 1000), 1.0)

    def test_double_price_scores_half(self):
        self.assertAlmostEqual(price_proximity(1000, 2000), 0.5)

    def test_zero_target_returns_neutral(self):
        self.assertEqual(price_proximity(0, 1000), 0.5)
        self.assertEqual(price_proximity(1000, 0), 0.5)


class DedupeTests(unittest.TestCase):

    def test_dedupe_excludes_target_and_dups(self):
        target_asin = "BTARGET"
        pool_a = [
            {"asin": "BA", "image": "x", "title": "a"},
            {"asin": "BTARGET", "image": "x", "title": "self"},
            {"asin": "BB", "image": "x", "title": "b"},
        ]
        pool_b = [
            {"asin": "BB", "image": "y", "title": "b_dup"},  # 重複 → 落ちる
            {"asin": "BC", "image": "y", "title": "c"},
            {"asin": "BD", "image": "", "title": "no-image"},  # image 無し → 落ちる
        ]
        out = dedupe_candidates([pool_a, pool_b], exclude_asin=target_asin)
        asins = [c["asin"] for c in out]
        self.assertEqual(asins, ["BA", "BB", "BC"])


class RankAndFloorTests(unittest.TestCase):

    def test_high_similarity_beats_pure_price_proximity(self):
        target = {"title": "レゴ シティ ポリスカー", "price": 3000}
        candidates = [
            {"asin": "B1", "title": "アンパンマン 太鼓", "price": 3000, "image": "x"},
            {"asin": "B2", "title": "レゴ シティ 消防車", "price": 8000, "image": "x"},
        ]
        ranked = rank_candidates(target, candidates, covered_asins=set())
        self.assertEqual(ranked[0]["asin"], "B2")  # タイトル類似が勝つ

    def test_internal_link_floor_injects_when_top_has_none(self):
        target = {"title": "レゴ シティ ポリスカー", "price": 3000}
        # top はどちらも非内部、ranked[2:] に内部リンク候補がいる
        ranked = [
            {"asin": "B1", "title": "レゴ シティ 消防車", "price": 3000, "image": "x"},
            {"asin": "B2", "title": "レゴ シティ 救急車", "price": 3100, "image": "x"},
            {"asin": "B3", "title": "レゴ シティ パトロール", "price": 4000, "image": "x"},
        ]
        covered = {"B3"}
        top = ensure_internal_link_floor(
            ranked, covered, total=2, min_internal=1, target=target,
        )
        self.assertIn("B3", [c["asin"] for c in top])

    def test_floor_does_not_inject_irrelevant_internal(self):
        target = {"title": "レゴ ブロック", "price": 3000}
        ranked = [
            {"asin": "B1", "title": "レゴ シティ", "price": 3000, "image": "x"},
            {"asin": "B2", "title": "ぬいぐるみ うさぎ もちもち", "price": 3000, "image": "x"},
        ]
        covered = {"B2"}  # 関連性 0 の内部リンク候補
        top = ensure_internal_link_floor(
            ranked, covered, total=1, min_internal=1, target=target,
        )
        # top=1 で B2 が internal でも関連性が低すぎるので B1 をキープ
        self.assertEqual(top[0]["asin"], "B1")


if __name__ == "__main__":
    unittest.main()
