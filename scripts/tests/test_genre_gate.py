"""Unit tests for genre_gate (#2823)。

カバレッジ:
1. classify_genre: root ベースの pass / flag / indeterminate (fail-open)
2. STORE_NODE_IDS: 販促ノードのみでは pass しない (Clover 糸通し型の誤 pass)
3. ALLOWED_NODE_IDS: root 対象外でも玩具相当ノードなら pass (ミニカー型の誤検知)
4. ALLOWED_ASINS: ノードを非玩具と共有する個別救済 (ぬいぐるみ/子ども手芸キット)
5. 2026-07-16 全量棚卸しの実データ回帰 (救済品が pass・削除品が flag のまま)
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from genre_gate import classify_genre  # noqa: E402

TOY_STORE = {"id": "8184989051", "name": "おもちゃ ストア", "root": "おもちゃ"}
TOY_GLOBAL = {"id": "5830559051", "name": "Toys - AmazonGlobal free shipping", "root": "おもちゃ"}
MINICAR = {"id": "2189574051", "name": "ミニカー・ダイキャストカー", "root": "ホビー"}
CRAFT_KIT = {"id": "3054990051", "name": "手芸キット", "root": "ホビー"}
FIGURE_OBJET = {"id": "10532098051", "name": "置物・オブジェ", "root": "ホーム＆キッチン"}
STICKER = {"id": "2189588051", "name": "シール・ステッカー", "root": "文房具・オフィス用品"}
BLOCKS = {"id": "2039115051", "name": "ブロック", "root": "おもちゃ"}
UNROOTED = {"id": "9999999051", "name": "root 未取得ノード", "root": None}


class TestRootClassification(unittest.TestCase):
    def test_allowed_root_passes(self):
        verdict, cat = classify_genre([BLOCKS])
        self.assertEqual(verdict, "pass")
        self.assertEqual(cat, [BLOCKS])

    def test_disallowed_root_flags(self):
        verdict, cat = classify_genre([STICKER])
        self.assertEqual(verdict, "flag")
        self.assertEqual(cat, [STICKER])

    def test_baby_root_variants_pass(self):
        for root in ("ベビー＆マタニティ", "ベビー&マタニティ"):
            with self.subTest(root=root):
                node = {"id": "1", "name": "おしゃぶり", "root": root}
                self.assertEqual(classify_genre([node])[0], "pass")

    def test_one_allowed_root_among_many_passes(self):
        self.assertEqual(classify_genre([STICKER, BLOCKS])[0], "pass")


class TestIndeterminate(unittest.TestCase):
    """root が取れないノードしか無い snapshot は fail-open で通す。"""

    def test_empty_is_indeterminate(self):
        self.assertEqual(classify_genre([]), ("indeterminate", []))

    def test_none_is_indeterminate(self):
        self.assertEqual(classify_genre(None), ("indeterminate", []))

    def test_unrooted_only_is_indeterminate(self):
        self.assertEqual(classify_genre([UNROOTED]), ("indeterminate", []))

    def test_store_nodes_only_is_indeterminate(self):
        """販促ノードは実カテゴリではないので判定材料にならない。"""
        self.assertEqual(classify_genre([TOY_STORE, TOY_GLOBAL]), ("indeterminate", []))

    def test_non_dict_entries_ignored(self):
        self.assertEqual(classify_genre(["ゴミ", None, 42]), ("indeterminate", []))


class TestStoreNodeExclusion(unittest.TestCase):
    def test_store_node_does_not_rescue_non_toy(self):
        """Clover 糸通し型: 「おもちゃ ストア」を持つ手芸用品は flag のまま。"""
        thread = {"id": "3054992051", "name": "糸通し・ひも通し", "root": "ホビー"}
        verdict, cat = classify_genre([TOY_STORE, TOY_GLOBAL, thread])
        self.assertEqual(verdict, "flag")
        self.assertEqual(cat, [thread], "販促ノードは判定対象カテゴリから除外される")


class TestAllowedNodeIds(unittest.TestCase):
    def test_minicar_node_passes_despite_hobby_root(self):
        """Matchbox/Hot Wheels 型: root=ホビーだが実態は玩具。"""
        self.assertEqual(classify_genre([TOY_STORE, MINICAR])[0], "pass")

    def test_allowed_node_passes_without_store_node(self):
        self.assertEqual(classify_genre([MINICAR])[0], "pass")

    def test_craft_kit_node_is_not_allowlisted(self):
        """手芸キットは大人向け裁縫道具 (B07TL3JZGH) と同ノードのため node 許可しない。"""
        self.assertEqual(classify_genre([TOY_STORE, CRAFT_KIT])[0], "flag")


class TestAllowedAsins(unittest.TestCase):
    def test_allowed_asin_passes(self):
        """B096TWPGWV: 手芸キットノードだが子ども向けなので ASIN 単位で救済。"""
        self.assertEqual(classify_genre([TOY_STORE, CRAFT_KIT], "B096TWPGWV")[0], "pass")

    def test_allowed_asin_passes_on_shared_node(self):
        """B0GC4MQL8N (ぬいぐるみ) は置物・オブジェを非玩具と共有する。"""
        self.assertEqual(classify_genre([FIGURE_OBJET], "B0GC4MQL8N")[0], "pass")

    def test_shared_node_still_flags_other_asin(self):
        """同じノードでも B0G4WCS2XX (キャップ) は救済されない。"""
        self.assertEqual(classify_genre([FIGURE_OBJET], "B0G4WCS2XX")[0], "flag")

    def test_allowed_asin_passes_even_with_no_category_nodes(self):
        """ASIN 救済は indeterminate より優先される。"""
        self.assertEqual(classify_genre([], "B0GC4MQL8N")[0], "pass")

    def test_unknown_asin_falls_through_to_node_rules(self):
        self.assertEqual(classify_genre([STICKER], "B00UNKNOWN1")[0], "flag")

    def test_asin_none_does_not_crash(self):
        self.assertEqual(classify_genre([BLOCKS], None)[0], "pass")


if __name__ == "__main__":
    unittest.main()
