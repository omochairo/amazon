"""yahoo_jan_bridge と resolve_ranking_asins.resolve_yahoo_jan のテスト (API は叩かない)。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import resolve_ranking_asins as rr  # noqa: E402
import yahoo_jan_bridge as yjb  # noqa: E402


class FakeAPI:
    """Creator API searchItems(keywords=JAN) の偽物。JAN→ASIN マップにあれば eans 一致 item を返す。"""

    def __init__(self, jan_to_asin: dict):
        self.jan_to_asin = jan_to_asin

    def search_items(self, keywords=None, **_kw):
        asin = self.jan_to_asin.get(keywords)
        items = [{"asin": "BNOISE0001",
                  "itemInfo": {"externalIds": {"eans": {"displayValues": ["4900000000000"]}}}}]
        if asin:
            items.append({"asin": asin,
                          "itemInfo": {"externalIds": {"eans": {"displayValues": [keywords]}}}})
        return {"searchResult": {"items": items}}

# チェックディジットの正しい JAN
JAN_A = "4904810000037"
JAN_B = "4904810000020"


class BuildQueryTest(unittest.TestCase):
    def test_strips_rakuten_promo(self):
        q = yjb.build_query(
            "全品予約販売＼楽天週間1位／ SNSで話題！！ CANPA 鉄棒 子供用 ぶら下がり鉄棒 折りたたみ")
        self.assertNotIn("楽天", q)
        self.assertNotIn("予約", q)
        self.assertIn("CANPA", q)

    def test_drops_release_date_and_review_bonus(self):
        q = yjb.build_query("9月26日発売 BOX レビュー特典付き 遊戯王 ORIGINAL ARTWORK COLLECTION ボックス")
        self.assertNotIn("9月26日", q)
        self.assertNotIn("レビュー", q)
        self.assertIn("遊戯王", q)

    def test_keeps_model_code_dropped_by_keyword_extractor(self):
        # extract_search_keyword は 21371 を落とす (2026-10-05 実測)。型番は補う。
        q = yjb.build_query("流通 限定商品 LEGO レゴ アイデア Wallace & Gromit 21371 おもちゃ 玩具 誕生日")
        self.assertIn("21371", q)

    def test_hyphenated_codes(self):
        self.assertIn("OP-17", yjb.build_query("ワンピース カードゲーム 世界最強の戦士 BOX ONE PIECE OP-17 バンダイ"))

    def test_empty(self):
        self.assertEqual(yjb.build_query(""), "")

    def test_keeps_title_in_kagi_brackets(self):
        self.assertIn("すみっコぐらし", yjb.build_query("「すみっコぐらし」 おしゃべり ぬいぐるみ"))

    def test_keeps_scale_but_drops_timed_date(self):
        q = yjb.build_query("10/4 20時〜先着 トミカ 1/64 スケール ミニカー")
        self.assertNotIn("10/4", q)
        q = yjb.build_query("ホットウィール 1/64 プレミアム カーカルチャー")
        self.assertIn("1/64", q)


class PickConsensusJanTest(unittest.TestCase):
    def test_majority_wins(self):
        hits = [{"janCode": JAN_A}, {"janCode": JAN_A}, {"janCode": JAN_B}, {"janCode": ""}]
        r = yjb.pick_consensus_jan(hits)
        self.assertEqual(r["jan"], JAN_A)

    def test_single_vote_is_rejected(self):
        r = yjb.pick_consensus_jan([{"janCode": JAN_A}, {"janCode": ""}])
        self.assertEqual(r["jan"], "")
        self.assertEqual(r["reason"], "too_few_votes")

    def test_tie_is_rejected(self):
        # 色違い・セット違いが同数並ぶ = どれが楽天の商品か決められない
        hits = [{"janCode": JAN_A}, {"janCode": JAN_B}, {"janCode": JAN_A}, {"janCode": JAN_B}]
        self.assertEqual(yjb.pick_consensus_jan(hits)["reason"], "tied")

    def test_bad_checksum_is_ignored(self):
        hits = [{"janCode": "4904810000038"}, {"janCode": "4904810000038"}]
        self.assertEqual(yjb.pick_consensus_jan(hits)["reason"], "no_jan_in_hits")

    def test_jan8_requires_checksum(self):
        good, bad = "49123456", "49123457"  # 4912345 のチェックディジットは 6
        self.assertEqual(yjb.pick_consensus_jan([{"janCode": good}] * 2)["jan"], good)
        self.assertEqual(yjb.pick_consensus_jan([{"janCode": bad}] * 2)["reason"], "no_jan_in_hits")

    def test_no_hits(self):
        self.assertEqual(yjb.pick_consensus_jan([])["reason"], "no_jan_in_hits")


class ResolveYahooJanTest(unittest.TestCase):
    def _items(self):
        return [
            {"rank": 7, "matched_asin": None, "title": "LEGO レゴ アイデア 21371"},
            {"rank": 9, "matched_asin": "B0MATCHED1", "title": "既に紐づいた商品"},
            # Search プール (rank なし) は対象外
            {"matched_asin": None, "title": "楽天 Search の商品", "source": "Rakuten"},
        ]

    def test_adopts_consensus_jan_that_matches_on_amazon(self):
        api = FakeAPI({JAN_A: "B0LEGO2137"})
        calls = []

        def hits(q):
            calls.append(q)
            return [{"janCode": JAN_A}, {"janCode": JAN_A}]

        r = rr.resolve_yahoo_jan(self._items(), api, set(), set(), hits, sleep=0)
        self.assertEqual(len(calls), 1)  # matched 済みと Search プールは問い合わせない
        e = r["yahoo_jan_entries"][0]
        self.assertTrue(e["adopted"])
        self.assertEqual((e["jan"], e["asin"]), (JAN_A, "B0LEGO2137"))
        self.assertEqual(r["yahoo_jan_adopted"], 1)

    def test_amazon_without_exact_jan_is_not_adopted(self):
        api = FakeAPI({})  # Amazon 側に JAN 一致なし
        r = rr.resolve_yahoo_jan(self._items(), api, set(), set(),
                                 lambda q: [{"janCode": JAN_A}] * 2, sleep=0)
        e = r["yahoo_jan_entries"][0]
        self.assertFalse(e["adopted"])
        self.assertEqual(e["reason"], "amazon_no_jan_match")

    def test_covered_seen_and_isbn_are_not_adopted(self):
        hits = lambda q: [{"janCode": JAN_A}] * 2  # noqa: E731
        r = rr.resolve_yahoo_jan(self._items(), FakeAPI({JAN_A: "B0LEGO2137"}),
                                 {"B0LEGO2137"}, set(), hits, sleep=0)
        self.assertEqual(r["yahoo_jan_entries"][0]["reason"], "already_covered")
        r = rr.resolve_yahoo_jan(self._items(), FakeAPI({JAN_A: "B0LEGO2137"}),
                                 set(), {"B0LEGO2137"}, hits, sleep=0)
        self.assertEqual(r["yahoo_jan_entries"][0]["reason"], "duplicate")
        r = rr.resolve_yahoo_jan(self._items(), FakeAPI({JAN_A: "9813379227"}),
                                 set(), set(), hits, sleep=0)
        self.assertEqual(r["yahoo_jan_entries"][0]["reason"], "isbn")

    def test_yahoo_error_is_recorded_and_does_not_stop(self):
        items = self._items() + [{"rank": 8, "matched_asin": None, "title": "BTM 三輪車 5in1"}]
        state = {"n": 0}

        def hits(q):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("503")
            return [{"janCode": JAN_B}] * 3

        r = rr.resolve_yahoo_jan(items, FakeAPI({JAN_B: "B0BTM00005"}), set(), set(), hits, sleep=0)
        self.assertEqual(r["yahoo_jan_entries"][0]["reason"], "yahoo_error: RuntimeError")
        self.assertTrue(r["yahoo_jan_entries"][1]["adopted"])

    def test_genre_gate_drop_goes_to_given_list(self):
        api = FakeAPI({JAN_A: "B0NOTTOY01"})
        dropped = []
        orig = rr._genre_verdict_for_item
        rr._genre_verdict_for_item = lambda item: ("flag", ["家電"])
        try:
            r = rr.resolve_yahoo_jan(self._items(), api, set(), set(),
                                     lambda q: [{"janCode": JAN_A}] * 2, sleep=0,
                                     genre_dropped=dropped)
        finally:
            rr._genre_verdict_for_item = orig
        self.assertEqual(r["yahoo_jan_entries"][0]["reason"], "genre_gate")
        self.assertEqual([d["match_method"] for d in dropped], ["yahoo_jan"])

    def test_limit(self):
        items = [{"rank": n, "matched_asin": None, "title": f"商品 {n}"} for n in range(1, 6)]
        r = rr.resolve_yahoo_jan(items, FakeAPI({}), set(), set(), lambda q: [], limit=2, sleep=0)
        self.assertEqual(r["yahoo_jan_candidates_before_limit"], 5)
        self.assertEqual(r["yahoo_jan_input"], 2)


if __name__ == "__main__":
    unittest.main()
