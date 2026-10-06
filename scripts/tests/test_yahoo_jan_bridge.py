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

    # 既定の Amazon 題名はブランド (レゴ) が判定できるもの = 同一性ガードを通る
    def __init__(self, jan_to_asin: dict, titles: dict | None = None,
                 default_title: str = "レゴ(LEGO) アイデア 21371"):
        self.jan_to_asin = jan_to_asin
        self.titles = titles or {}
        self.default_title = default_title

    def search_items(self, keywords=None, **_kw):
        asin = self.jan_to_asin.get(keywords)
        items = [{"asin": "BNOISE0001",
                  "itemInfo": {"externalIds": {"eans": {"displayValues": ["4900000000000"]}}}}]
        if asin:
            items.append({"asin": asin, "itemInfo": {
                "title": {"displayValue": self.titles.get(asin, self.default_title)},
                "externalIds": {"eans": {"displayValues": [keywords]}}}})
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
    @staticmethod
    def _hits(a, b=0):
        return [{"janCode": JAN_A}] * a + [{"janCode": JAN_B}] * b + [{"janCode": ""}]

    def test_majority_wins(self):
        self.assertEqual(yjb.pick_consensus_jan(self._hits(4, 1))["jan"], JAN_A)

    def test_two_votes_are_too_few(self):
        # 2026-10-06 run のノーブランド飛び石は 2 対 1
        self.assertEqual(yjb.pick_consensus_jan(self._hits(2, 1))["reason"], "too_few_votes")

    def test_close_second_is_rejected(self):
        # 同 run の実測: 年式違い 10 対 9・単品とセット 10 対 6 は落とし、13 対 6 は採る
        self.assertEqual(yjb.pick_consensus_jan(self._hits(10, 9))["reason"], "close_second")
        self.assertEqual(yjb.pick_consensus_jan(self._hits(10, 6))["reason"], "close_second")
        self.assertEqual(yjb.pick_consensus_jan(self._hits(13, 6))["jan"], JAN_A)
        self.assertEqual(yjb.pick_consensus_jan(self._hits(5, 1))["jan"], JAN_A)

    def test_single_vote_is_rejected(self):
        r = yjb.pick_consensus_jan([{"janCode": JAN_A}, {"janCode": ""}])
        self.assertEqual(r["jan"], "")
        self.assertEqual(r["reason"], "too_few_votes")

    def test_tie_is_rejected(self):
        # 色違い・セット違いが同数並ぶ = どれが楽天の商品か決められない
        self.assertEqual(yjb.pick_consensus_jan(self._hits(4, 4))["reason"], "tied")

    def test_bad_checksum_is_ignored(self):
        hits = [{"janCode": "4904810000038"}, {"janCode": "4904810000038"}]
        self.assertEqual(yjb.pick_consensus_jan(hits)["reason"], "no_jan_in_hits")

    def test_jan8_requires_checksum(self):
        good, bad = "49123456", "49123457"  # 4912345 のチェックディジットは 6
        self.assertEqual(yjb.pick_consensus_jan([{"janCode": good}] * 3)["jan"], good)
        self.assertEqual(yjb.pick_consensus_jan([{"janCode": bad}] * 3)["reason"], "no_jan_in_hits")

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
            return [{"janCode": JAN_A}] * 3

        r = rr.resolve_yahoo_jan(self._items(), api, set(), set(), hits, sleep=0)
        self.assertEqual(len(calls), 1)  # matched 済みと Search プールは問い合わせない
        e = r["yahoo_jan_entries"][0]
        self.assertTrue(e["adopted"])
        self.assertEqual((e["jan"], e["asin"]), (JAN_A, "B0LEGO2137"))
        self.assertEqual(r["yahoo_jan_adopted"], 1)

    def test_amazon_without_exact_jan_is_not_adopted(self):
        api = FakeAPI({})  # Amazon 側に JAN 一致なし
        r = rr.resolve_yahoo_jan(self._items(), api, set(), set(),
                                 lambda q: [{"janCode": JAN_A}] * 3, sleep=0)
        e = r["yahoo_jan_entries"][0]
        self.assertFalse(e["adopted"])
        self.assertEqual(e["reason"], "amazon_no_jan_match")

    def test_covered_seen_and_isbn_are_not_adopted(self):
        hits = lambda q: [{"janCode": JAN_A}] * 3  # noqa: E731
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
        items = self._items() + [{"rank": 8, "matched_asin": None, "title": "レゴ アイデア 21371 別ショップ"}]
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
                                     lambda q: [{"janCode": JAN_A}] * 3, sleep=0,
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


class IdentityGuardTest(unittest.TestCase):
    """オーナー判断 (2026-10-06): ノーブランド品はブランドか型番が一致しない限り採らない。"""

    def _run(self, rakuten_title, amazon_title):
        items = [{"rank": 1, "itemCode": "shop:1", "matched_asin": None, "title": rakuten_title}]
        api = FakeAPI({JAN_A: "B0TEST0001"}, default_title=amazon_title)
        r = rr.resolve_yahoo_jan(items, api, set(), set(), lambda q: [{"janCode": JAN_A}] * 3, sleep=0)
        return r["yahoo_jan_entries"][0]

    def test_nobrand_without_model_is_rejected(self):
        # 2026-10-05 dry-run のバランスストーン: 似た商品を複数メーカーが出している
        e = self._run("バランスストーン 平均台 飛び石 セット 子供", "KIDS バランスストーン 6個セット")
        self.assertFalse(e["adopted"])
        self.assertEqual(e["reason"], "no_brand_or_model_match")
        self.assertFalse(e["identity_ok"])

    def test_nobrand_with_common_model_code_is_adopted(self):
        e = self._run("ワンピース カードゲーム 世界最強の戦士 BOX OP-17", "ワンピース カードゲーム OP-17 BOX")
        self.assertTrue(e["adopted"])
        self.assertEqual(e["common_codes"], ["OP-17"])

    def test_same_brand_is_adopted_without_model(self):
        e = self._run("変身ベルト DXマイスドライバー 仮面ライダーマイス＆リドセット バンダイ",
                      "[BANDAI] 変身ベルト DXマイスドライバー 仮面ライダーマイス＆リドセット")
        self.assertTrue(e["adopted"])
        self.assertEqual((e["rakuten_brand"], e["amazon_brand"]), ("バンダイ", "バンダイ"))

    def test_amazon_only_brand_needs_model(self):
        # 楽天のノーブランド汎用品の JAN が多数決で有名メーカー品に当たったケース
        e = self._run("バランスストーン 平均台 飛び石 セット 子供", "バンダイ ストーンバランス")
        self.assertFalse(e["adopted"])
        self.assertEqual(e["reason"], "no_brand_or_model_match")

    def test_conflicting_brands_are_rejected(self):
        e = self._run("タカラトミー トミカ No.1 日産 GT-R", "バンダイ ミニカー GT-R")
        self.assertEqual(e["reason"], "brand_mismatch")


class ModelCodesTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(yjb.model_codes("LEGO 21371 / OP-17 / DM26-EX4 2026年"),
                         {"21371", "OP-17", "DM26-EX4"})

    def test_prices_and_capacities_are_not_codes(self):
        self.assertEqual(yjb.model_codes("10000円 10000mAh 12,800 100000 ピース"), set())


class RankingPageCacheTest(unittest.TestCase):
    def test_matches_include_covered_but_not_unsafe(self):
        res = {"yahoo_jan_entries": [
            {"item_code": "a:1", "asin": "B0COVERED1", "jan": JAN_A, "identity_ok": True,
             "reason": "already_covered"},
            {"item_code": "a:2", "asin": "B0NEW00001", "jan": JAN_B, "identity_ok": True, "reason": ""},
            {"item_code": "a:3", "asin": "B0NOBRAND1", "jan": JAN_A, "identity_ok": False,
             "reason": "nobrand_without_model_match"},
            {"item_code": "a:4", "asin": "B0NOTTOY01", "jan": JAN_A, "identity_ok": True,
             "reason": "genre_gate"},
            {"item_code": "", "asin": "B0NOCODE01", "jan": JAN_A, "identity_ok": True, "reason": ""},
        ]}
        m = rr.ranking_page_matches(res, "2026-10-06T00:00:00+00:00")
        self.assertEqual(sorted(m), ["a:1", "a:2"])
        self.assertEqual(m["a:1"]["asin"], "B0COVERED1")

    def test_merge_overwrites_and_prunes_old(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)
        existing = {
            "old:1": {"asin": "B0OLD00001", "checked_at": "2026-08-01T00:00:00+00:00"},
            "keep:1": {"asin": "B0KEEP0001", "checked_at": "2026-10-01T00:00:00+00:00"},
            "upd:1": {"asin": "B0BEFORE01", "checked_at": "2026-10-01T00:00:00+00:00"},
            "bad:1": {"asin": "B0BAD00001", "checked_at": "not-a-date"},
        }
        new = {"upd:1": {"asin": "B0AFTER001", "checked_at": now.isoformat()}}
        merged = rr.merge_ranking_page_cache(existing, new, now)
        self.assertEqual(sorted(merged), ["keep:1", "upd:1"])
        self.assertEqual(merged["upd:1"]["asin"], "B0AFTER001")


if __name__ == "__main__":
    unittest.main()
