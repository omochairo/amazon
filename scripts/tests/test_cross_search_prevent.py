"""上流マッチャの誤マッチ除外 (#2186 P1 prevent) のユニットテスト。

check_no_reseller_pricing (catch 側) が反応する前に、cross-source マッチ生成
段階で「中古/ジャンク出品」「型番不一致の別商品 (書籍等)」を弾けることを検証する。
"""

from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_cross_search as fcs  # noqa: E402


class IsUsedListingTest(unittest.TestCase):
    def test_used_keywords_detected(self):
        for t in (
            "【中古】Hape 木製パズル",
            "Hape ジャンク品 まとめ売り",
            "訳あり アウトレット 知育玩具",
            "リユース品 ベイブレード",
            "USED Tomica Set",
        ):
            self.assertTrue(fcs._is_used_listing(t), t)

    def test_new_listing_not_flagged(self):
        for t in ("Hape E3209 木製パズル 新品", "タカラトミー トミカ", ""):
            self.assertFalse(fcs._is_used_listing(t), t)


class ModelConsistencyTest(unittest.TestCase):
    def test_extract_model_tokens(self):
        self.assertEqual(fcs._extract_model_tokens("ハペ E3209 木製"), {"E3209"})
        # ハイフン入り型番は除去版も返す
        self.assertEqual(
            fcs._extract_model_tokens("パナソニック EH-2310 おもちゃ"),
            {"EH-2310", "EH2310"},
        )
        # 英字のみ (TOMICA) は型番扱いしない
        self.assertEqual(fcs._extract_model_tokens("TOMICA トミカ"), set())

    def test_consistent_when_model_matches(self):
        self.assertTrue(
            fcs._title_model_consistent("Hape E3209 木製", "【楽天】Hape E3209 パズル")
        )

    def test_inconsistent_when_model_absent_in_candidate(self):
        # B097BP639K 系: 型番ありの玩具に対し Yahoo が書籍を誤マッチ
        self.assertFalse(
            fcs._title_model_consistent("Hape E3209 木製", "絵本 はらぺこあおむし")
        )

    def test_no_model_skips_check(self):
        # 型番が無い商品は判定不能 → over-filter を避けて通す
        self.assertTrue(
            fcs._title_model_consistent("メロディーゴーラウンド", "全然違う本")
        )

    def test_hyphen_normalization(self):
        self.assertTrue(
            fcs._title_model_consistent("EH-2310 おもちゃ", "EH2310 楽天ストア")
        )


class FilterTextCandidatesTest(unittest.TestCase):
    def setUp(self):
        fcs._DROP_STATS.clear()

    def _items(self, *titles):
        return [{"title": t, "price": 1000} for t in titles]

    def test_drops_used_and_mismatch_keeps_good(self):
        items = self._items(
            "Hape E3209 木製パズル",        # keep
            "【中古】Hape E3209",           # drop: used
            "絵本 E1234 別商品",            # drop: model mismatch
        )
        kept = fcs._filter_text_candidates(items, "Hape E3209 木製", "yahoo", "B0X")
        self.assertEqual([i["title"] for i in kept], ["Hape E3209 木製パズル"])
        self.assertEqual(fcs._DROP_STATS["yahoo:used_listing"], 1)
        self.assertEqual(fcs._DROP_STATS["yahoo:model_mismatch"], 1)

    def test_empty_title_skips_model_filter(self):
        # JAN 段は amazon_title="" で呼ぶ → used のみ落とし、型番判定はしない
        items = self._items("Hape E3209", "【中古】Hape E3209")
        kept = fcs._filter_text_candidates(items, "", "rakuten", "B0X")
        self.assertEqual(len(kept), 1)
        self.assertEqual(fcs._DROP_STATS["rakuten:used_listing"], 1)
        self.assertNotIn("rakuten:model_mismatch", fcs._DROP_STATS)

    def test_no_model_does_not_overfilter(self):
        # 型番無し商品では候補をひとつも落とさない (drop-overfilter 教訓)
        items = self._items("メロディーゴーラウンド 楽天", "クラシックドラム 別店")
        kept = fcs._filter_text_candidates(items, "メロディーゴーラウンド", "yahoo", "B0X")
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()


class ReSearchWindowTest(unittest.TestCase):
    """低信頼再検索の刻み (#5047 follow-up)。

    このレーンは cron を持たない手動メンテ専用で、2026-05-30 の次が 2026-09-06
    だった。3 か月分溜まって 1 run が 2 時間超になり打ち切りになったので、
    offset + 上限で刻めるようにした。
    """

    def test_unlimited_by_default(self):
        """max_n=0 / offset=0 は既存挙動 (全件) を変えない。"""
        for idx in (0, 1, 500, 99999):
            self.assertTrue(fcs._re_search_window(idx, 0, 0), idx)

    def test_limit_caps_the_run(self):
        for idx in range(500):
            self.assertTrue(fcs._re_search_window(idx, 0, 500), idx)
        for idx in (500, 501, 1000):
            self.assertFalse(fcs._re_search_window(idx, 0, 500), idx)

    def test_offset_skips_the_head(self):
        """offset 未満は窓外。上限だけでは末尾に到達しない問題への対処。"""
        for idx in (0, 499):
            self.assertFalse(fcs._re_search_window(idx, 500, 500), idx)
        for idx in (500, 999):
            self.assertTrue(fcs._re_search_window(idx, 500, 500), idx)
        for idx in (1000, 1500):
            self.assertFalse(fcs._re_search_window(idx, 500, 500), idx)

    def test_offset_without_limit_takes_the_tail(self):
        self.assertFalse(fcs._re_search_window(499, 500, 0))
        for idx in (500, 5000):
            self.assertTrue(fcs._re_search_window(idx, 500, 0), idx)

    def test_windows_tile_without_gap_or_overlap(self):
        """0/500 -> 500/500 -> 1000/500 で全件をちょうど 1 回ずつ覆う。"""
        n, step = 1300, 500
        covered = []
        for offset in range(0, n, step):
            covered += [i for i in range(n) if fcs._re_search_window(i, offset, step)]
        self.assertEqual(sorted(covered), list(range(n)))
        self.assertEqual(len(covered), len(set(covered)))
