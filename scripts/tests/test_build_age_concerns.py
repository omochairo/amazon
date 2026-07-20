"""Unit tests for build_age_concerns.py (Issue #3332 N2 / #2687 柱1).

Coverage:
1. _hub_key_for_theme - age-5 -> age-4 統合、age-0 恒等写像、programming/
   english/None の除外。
2. classify_intent    - 安全/選び方・量/困りごと の3分類。
3. rank_concerns       - impressions>0 が先頭、同点は入力順 (gap 順) 維持。
4. group_concerns      - age-2 のみ intent 分類、他 hub は単一グループ、
   固定順 (困りごと→安全→選び方・量)、空 intent は出さない。
5. run                 - end-to-end: age-5 統合・除外テーマ・出力 JSON 形状・
   demand_gaps.json 不在時の graceful 空出力 (exit 0 相当)。
"""
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

import build_age_concerns as bac  # noqa: E402


def _gap(query, theme_key, impressions=0, nearest_asin="B0X", nearest_title="タイトル"):
    return {
        "query": query,
        "sources": ["suggest"],
        "impressions": impressions,
        "theme_key": theme_key,
        "max_sim": 0.5,
        "nearest_asin": nearest_asin,
        "nearest_title": nearest_title,
    }


class HubKeyForThemeTest(unittest.TestCase):
    def test_age5_maps_to_age4(self):
        self.assertEqual(bac._hub_key_for_theme("age-5"), "age-4")

    def test_identity_for_known_ages(self):
        for k in ["age-1", "age-2", "age-3", "age-4", "age-6"]:
            self.assertEqual(bac._hub_key_for_theme(k), k)

    def test_age0_passthrough(self):
        self.assertEqual(bac._hub_key_for_theme("age-0"), "age-0")

    def test_excludes_non_age_themes(self):
        self.assertIsNone(bac._hub_key_for_theme("programming"))
        self.assertIsNone(bac._hub_key_for_theme("english"))
        self.assertIsNone(bac._hub_key_for_theme(None))
        self.assertIsNone(bac._hub_key_for_theme(""))


class ClassifyIntentTest(unittest.TestCase):
    def test_safety_bucket(self):
        self.assertEqual(bac.classify_intent("2歳 おもちゃ 口に入れる"), "安全")
        self.assertEqual(bac.classify_intent("誤飲 対策 おもちゃ"), "安全")
        self.assertEqual(bac.classify_intent("積み木 なめる"), "安全")

    def test_choice_bucket(self):
        self.assertEqual(bac.classify_intent("おもちゃ 選び方 2歳"), "選び方・量")
        self.assertEqual(bac.classify_intent("おもちゃ 何個 与える"), "選び方・量")

    def test_default_bucket(self):
        self.assertEqual(bac.classify_intent("おもちゃ 投げる 壊す"), "困りごと")
        self.assertEqual(bac.classify_intent("おもちゃ 取り合い 兄弟"), "困りごと")


class RankConcernsTest(unittest.TestCase):
    def test_impressions_desc_stable_ties(self):
        entries = [
            {"query": "a", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
            {"query": "b", "impressions": 5, "nearest_asin": "", "nearest_title": "", "source": "gsc"},
            {"query": "c", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
            {"query": "d", "impressions": 5, "nearest_asin": "", "nearest_title": "", "source": "gsc"},
        ]
        out = bac.rank_concerns(entries)
        # impressions>0 (b, d) 先頭。同点(b,d)・(a,c)内は入力順維持。
        self.assertEqual([e["query"] for e in out], ["b", "d", "a", "c"])


class GroupConcernsTest(unittest.TestCase):
    def test_age2_intent_split_fixed_order(self):
        entries = [
            {"query": "投げる おもちゃ", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
            {"query": "口に入れる 対策", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
            {"query": "選び方 わからない", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
        ]
        groups = bac.group_concerns("age-2", entries)
        intents = [g["intent"] for g in groups]
        self.assertEqual(intents, ["困りごと", "安全", "選び方・量"])
        self.assertEqual(len(groups[0]["concerns"]), 1)
        self.assertEqual(len(groups[1]["concerns"]), 1)
        self.assertEqual(len(groups[2]["concerns"]), 1)

    def test_non_age2_single_group(self):
        entries = [
            {"query": "口に入れる 対策", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
            {"query": "選び方 わからない", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
        ]
        groups = bac.group_concerns("age-3", entries)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["intent"], "困りごと")
        self.assertEqual(len(groups[0]["concerns"]), 2)

    def test_empty_intent_omitted(self):
        entries = [
            {"query": "投げる おもちゃ", "impressions": 0, "nearest_asin": "", "nearest_title": "", "source": "suggest"},
        ]
        groups = bac.group_concerns("age-2", entries)
        self.assertEqual([g["intent"] for g in groups], ["困りごと"])


class RunTest(unittest.TestCase):
    def test_end_to_end_age5_merge_and_theme_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            demand_gaps = pathlib.Path(tmp) / "demand_gaps.json"
            out_hugo = pathlib.Path(tmp) / "concerns"
            payload = {
                "generated_at": "2026-07-20T00:00:00Z",
                "params": {},
                "summary": {},
                "gaps": [
                    _gap("2歳 おもちゃ 投げる", "age-2", impressions=0),
                    _gap("2歳 おもちゃ 壊す", "age-2", impressions=10),
                    _gap("2歳 おもちゃ 口に入れる", "age-2", impressions=0),
                    _gap("2歳 おもちゃ 選び方", "age-2", impressions=0),
                    _gap("5歳 おもちゃ 飽きる", "age-5", impressions=0),
                    _gap("プログラミング 用語", "programming", impressions=0),
                    _gap("英語 翻訳", "english", impressions=0),
                    _gap("謎クエリ", None, impressions=100),
                ],
            }
            demand_gaps.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            counts = bac.run(demand_gaps, out_hugo)

            # age-5 は age-4 に統合される -> age-4.json が存在し1件。
            self.assertEqual(counts.get("age-4"), 1)
            self.assertEqual(counts.get("age-2"), 4)
            self.assertNotIn("programming", counts)
            self.assertNotIn("english", counts)
            self.assertNotIn("age-0", counts)  # 該当 gap なし

            age4_data = json.loads((out_hugo / "age-4.json").read_text(encoding="utf-8"))
            self.assertEqual(age4_data["type"], "age-4")
            self.assertEqual(age4_data["count"], 1)
            self.assertEqual(age4_data["groups"][0]["concerns"][0]["query"], "5歳 おもちゃ 飽きる")

            age2_data = json.loads((out_hugo / "age-2.json").read_text(encoding="utf-8"))
            self.assertEqual(age2_data["count"], 4)
            groups_by_intent = {g["intent"]: g["concerns"] for g in age2_data["groups"]}
            # 困りごと (投げる/壊す) 内で impressions=10 の「壊す」がランクで先頭。
            self.assertEqual(
                [c["query"] for c in groups_by_intent["困りごと"]],
                ["2歳 おもちゃ 壊す", "2歳 おもちゃ 投げる"],
            )
            self.assertEqual(
                [c["query"] for c in groups_by_intent["安全"]], ["2歳 おもちゃ 口に入れる"]
            )
            self.assertEqual(
                [c["query"] for c in groups_by_intent["選び方・量"]], ["2歳 おもちゃ 選び方"]
            )
            # groups の固定順 (困りごと→安全→選び方・量)
            self.assertEqual(
                [g["intent"] for g in age2_data["groups"]], ["困りごと", "安全", "選び方・量"]
            )
            # groups 形状 (intent/concerns キー)
            for g in age2_data["groups"]:
                self.assertIn("intent", g)
                self.assertIn("concerns", g)
                for c in g["concerns"]:
                    self.assertIn("query", c)
                    self.assertIn("nearest_asin", c)
                    self.assertIn("nearest_title", c)
                    self.assertIn("impressions", c)
                    self.assertIn("source", c)

            self.assertFalse((out_hugo / "programming.json").exists())
            self.assertFalse((out_hugo / "english.json").exists())

    def test_missing_demand_gaps_is_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            demand_gaps = pathlib.Path(tmp) / "does_not_exist.json"
            out_hugo = pathlib.Path(tmp) / "concerns"
            counts = bac.run(demand_gaps, out_hugo)
            self.assertEqual(counts, {})
            self.assertEqual(list(out_hugo.glob("*.json")), [])

    def test_malformed_demand_gaps_is_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            demand_gaps = pathlib.Path(tmp) / "demand_gaps.json"
            demand_gaps.write_text("{not valid json", encoding="utf-8")
            out_hugo = pathlib.Path(tmp) / "concerns"
            counts = bac.run(demand_gaps, out_hugo)
            self.assertEqual(counts, {})

    def test_cli_help_exits_zero(self):
        with self.assertRaises(SystemExit) as cm:
            bac.main(["--help"])
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
