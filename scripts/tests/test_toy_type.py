"""scripts/toy_type.py (おもちゃの種類の判定。navi-brain#165 T2) のテスト。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import toy_type  # noqa: E402


class LoadRulesTests(unittest.TestCase):
    def test_yaml_has_expected_types(self):
        rules = toy_type.load_rules()
        for key in ("puzzle", "blocks", "vehicles", "learning", "action"):
            self.assertIn(key, rules.keys)
        # 学習・対戦/ブラスターも種類に入れる (設計の決定)
        self.assertEqual(toy_type.label("action"), "対戦・ブラスター")
        for key in rules.keys:
            self.assertTrue(rules.keywords[key], key)


class ClassifyTests(unittest.TestCase):
    def test_node_layer_wins_over_tags(self):
        types, src = toy_type.classify(["ジグソーパズル"], ["ブロック"], "なにか")
        self.assertEqual((types, src), (["puzzle"], "node"))

    def test_falls_back_to_tags_then_name(self):
        self.assertEqual(toy_type.classify(["赤ちゃん・幼児のおもちゃ"], ["ナーフ"], ""), (["action"], "tag"))
        self.assertEqual(toy_type.classify([], ["知育玩具"], "木製 型はめ ボックス"), (["puzzle"], "name"))

    def test_unknown_is_empty(self):
        self.assertEqual(toy_type.classify(["知育・学習玩具"], ["知育玩具", "プレゼント"], "ミラー"), ([], "none"))

    def test_multiple_types_in_yaml_order(self):
        types, _ = toy_type.classify(["大工さん", "ブロック"], [], "")
        self.assertEqual(types, ["blocks", "pretend"])

    def test_broad_words_do_not_misfire(self):
        # 「ロボット・ソフビ人形」は人形 (科学ではない)、「ベビー用ボール」は赤ちゃん (外遊びではない)
        self.assertEqual(toy_type.classify(["ロボット・ソフビ人形"], [], "")[0], ["figures"])
        self.assertEqual(toy_type.classify(["ベビー用ボール"], [], "")[0], ["baby"])
        self.assertEqual(toy_type.classify(["メイキングトイ"], [], "")[0], ["craft"])
        self.assertEqual(toy_type.classify(["タブレット用アクセサリ"], [], "")[0], [])


class BrowseNodeNamesTests(unittest.TestCase):
    def test_skips_store_noise_and_broad_nodes(self):
        item = {"browse_nodes": [
            {"id": "8184989051", "name": "パズル"},             # STORE_NODE_IDS
            {"id": "1", "name": "おもちゃ_2column"},             # 売り場
            {"id": "2", "name": "2189078051,2189163051"},       # ID の羅列
            {"id": "3", "name": "ブロック・パズル・ゲーム"},     # 大分類
            {"id": "4", "name": "STEM Toys ~理系脳を育むおもちゃ~"},  # 企画売り場
            {"id": "5", "name": "スポンジ銃"},
            "not a dict",
        ]}
        self.assertEqual(toy_type.browse_node_names(item), ["スポンジ銃"])

    def test_keeps_names_starting_with_digits(self):
        # ID の羅列だけを外し、「3Dパズル」のような普通の名前は残す (agy レビューの指摘)
        item = {"browse_nodes": [{"id": "1", "name": "3Dパズル"}, {"id": "2", "name": "2189602051 game"}]}
        self.assertEqual(toy_type.browse_node_names(item), ["3Dパズル"])

    def test_missing_item(self):
        self.assertEqual(toy_type.browse_node_names(None), [])
        self.assertEqual(toy_type.browse_node_names({"browse_nodes": "x"}), [])

    def test_classify_article(self):
        data = {"product": {"name": "x"}, "tags": ["パズル"]}
        self.assertEqual(toy_type.classify_article(data, {"browse_nodes": [{"id": "9", "name": "ミニカー"}]}),
                         ["vehicles"])
        self.assertEqual(toy_type.classify_article(data, None), ["puzzle"])


if __name__ == "__main__":
    unittest.main()
