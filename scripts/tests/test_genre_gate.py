"""Unit tests for genre_gate (#2823)。

カバレッジ:
1. classify_genre: root ベースの pass / flag / indeterminate (fail-open)
2. STORE_NODE_IDS: 販促ノードのみでは pass しない (Clover 糸通し型の誤 pass)
3. ALLOWED_NODE_IDS: root 対象外でも玩具相当ノードなら pass (ミニカー型の誤検知)
4. ALLOWED_ASINS: ノードを非玩具と共有する個別救済 (ぬいぐるみ/子ども手芸キット)
5. 2026-07-16 全量棚卸しの実データ回帰 (救済品が pass・削除品が flag のまま)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from genre_gate import classify_genre, is_flagged_snapshot  # noqa: E402

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


class IsFlaggedSnapshotTest(unittest.TestCase):
    """#9155: first-party の pick 時に per_asin の snapshot で判定する。"""

    BLEACH = {"id": "170563011", "name": "漂白剤", "root": "ドラッグストア"}

    def _snap(self, root, asin, body):
        d = os.path.join(root, asin)
        os.makedirs(d)
        with open(os.path.join(d, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)

    def test_snapshot_verdicts(self):
        with tempfile.TemporaryDirectory() as root:
            self._snap(root, "B000FQMTIU", {"asin": "B000FQMTIU",
                                            "item": {"title": "キッチンハイター", "browse_nodes": [self.BLEACH]}})
            self._snap(root, "B0TOY00001", {"asin": "B0TOY00001",
                                            "item": {"title": "ブロック", "browse_nodes": [BLOCKS]}})
            self._snap(root, "B0MISS0001", {"asin": "B0MISS0001", "status": "gone", "miss_count": 3})
            self._snap(root, "B0OLD00001", {"asin": "B0OLD00001",
                                            "item": {"title": "旧 snapshot", "browse_nodes": [UNROOTED]}})
            self.assertTrue(is_flagged_snapshot(root, "B000FQMTIU"))
            self.assertFalse(is_flagged_snapshot(root, "B0TOY00001"))
            # データ無し・root 未取得・ファイル無しは fail-open で False
            self.assertFalse(is_flagged_snapshot(root, "B0MISS0001"))
            self.assertFalse(is_flagged_snapshot(root, "B0OLD00001"))
            self.assertFalse(is_flagged_snapshot(root, "B0NOFILE01"))
            # browse_nodes の形が壊れていても例外にせず False (pick を止めない)
            self._snap(root, "B0BROKEN01", {"asin": "B0BROKEN01",
                                            "item": {"title": "壊れ", "browse_nodes": 5}})
            self.assertFalse(is_flagged_snapshot(root, "B0BROKEN01"))

    def test_rescued_kids_items_from_2026_10_08_audit(self):
        home = {"id": "3839151", "name": "ホームストア", "root": "ホーム＆キッチン"}
        for asin in ("B0FTFDRP62", "B089Y3VR2Q", "B0BJK8DGPG", "B082HX5B97"):
            with self.subTest(asin=asin):
                self.assertEqual(classify_genre([home], asin)[0], "pass")
        self.assertEqual(classify_genre([home], "B00ZF3P72S")[0], "flag")  # IKEA 収納家具は削除側


if __name__ == "__main__":
    unittest.main()
