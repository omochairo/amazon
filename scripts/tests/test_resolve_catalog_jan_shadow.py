"""resolve_catalog_jan_shadow.py の単体テスト (Issue #3561 shadow レーン)。

Rakuten Product/Search・Creator API とも一切呼ばない (全 mock)。カバレッジ:
1. build_catalog_queries: 識別力順 (型番→ブランド→先頭トークン) / 32字詰め / 記号除去 / リトライ2段
2. evaluate_catalog_candidate: G1 (brand) / G2 (productNo優先・token fallback) / G4 (ISBN)
3. G4 実測ケース: 「ベイブレード」→ ISBN 9784091541376 を reject すること
4. collect_shadow_population: JAN 抽出可能 item の除外 / title_fuzzy 既解決 rank の除外
5. process_item / run_shadow: Stage R→ガードレール→Stage A→G3/ジャンルゲートの一気通貫と manifest 形状
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import resolve_catalog_jan_shadow as shadow  # noqa: E402


class BuildCatalogQueriesTest(unittest.TestCase):
    def test_prioritizes_model_token_then_brand_then_leading_tokens(self):
        # レゴ(LEGO) (brand_taxonomy 収録・"レゴ"単体は fold 後 4字未満で fuzzy 対象外
        # のため "LEGO" alias を含む表記にする) + 型番 40499 を含むタイトル。
        title = "レゴ(LEGO) クラシック 基礎ブロック 40499 大きな缶"
        queries = shadow.build_catalog_queries(title)
        self.assertTrue(queries)
        stage, query = queries[0]
        self.assertEqual(stage, "primary")
        # 型番トークンが最優先で query 先頭に来ること。
        self.assertTrue(query.startswith("40499"))
        # ブランド語 (canonical) が primary query に含まれること。
        self.assertIn("レゴ", query)

    def test_truncates_to_32_chars_without_breaking_tokens(self):
        title = "アガツマ アンパンマン だいすき! ぐるぐるチャイム DX 長いタイトルのおもちゃです ほにゃらら"
        queries = shadow.build_catalog_queries(title)
        _, primary = queries[0]
        self.assertLessEqual(len(primary), 32)

    def test_strips_decoration_brackets(self):
        title = "【送料無料】【あす楽】レゴ 40499 基礎ブロック"
        queries = shadow.build_catalog_queries(title)
        for _, q in queries:
            self.assertNotIn("【", q)
            self.assertNotIn("】", q)

    def test_retry_stages_are_model_only_then_brand_plus_two(self):
        title = "レゴ クラシック 基礎ブロック 40499 大きな缶"
        queries = shadow.build_catalog_queries(title)
        stages = [s for s, _ in queries]
        self.assertIn("primary", stages)
        # 型番トークンがあるので retry_model 段が存在する。
        self.assertIn("retry_model", stages)
        retry_model_query = dict(queries)["retry_model"]
        self.assertEqual(retry_model_query, "40499")

    def test_no_more_than_two_retries(self):
        title = "レゴ クラシック 基礎ブロック 40499 大きな缶"
        queries = shadow.build_catalog_queries(title)
        # primary + 最大2リトライ = 3件まで。
        self.assertLessEqual(len(queries), 3)

    def test_empty_title_returns_no_queries(self):
        self.assertEqual(shadow.build_catalog_queries(""), [])

    def test_duplicate_retry_queries_are_skipped(self):
        # 型番トークンが無いタイトルでは retry_model 段は生成されない。
        title = "ノーブランドのぬいぐるみ くま"
        queries = shadow.build_catalog_queries(title)
        stages = [s for s, _ in queries]
        self.assertNotIn("retry_model", stages)


class EvaluateCatalogCandidateTest(unittest.TestCase):
    def test_g1_g2_pass_with_product_no(self):
        # "レゴ" 単体は fold 後 4字未満で fuzzy 対象外のため、listing 側も "LEGO" alias
        # を含む表記にする (実際の楽天タイトルでも "レゴ(LEGO)" 表記が一般的)。
        listing_title = "レゴ(LEGO) クラシック 基礎ブロック 40499 大きな缶"
        product = {
            "productName": "レゴ (LEGO) クラシック 基礎ブロック <大きな缶> 40499",
            "productNo": "40499",
            "makerCode": "",
            "makerName": "レゴジャパン",
            "productCode": "5702016995992",
        }
        ev = shadow.evaluate_catalog_candidate(listing_title, product)
        self.assertTrue(ev["g1_brand_match"])
        self.assertTrue(ev["g2_model_match"])
        self.assertEqual(ev["g2_method"], "productNo")
        self.assertFalse(ev["g4_is_isbn"])
        self.assertTrue(ev["passed_stage_r"])

    def test_g1_fails_on_brand_mismatch(self):
        listing_title = "レゴ(LEGO) クラシック 基礎ブロック 40499"
        product = {
            "productName": "タカラトミー プラレール 40499",
            "productNo": "40499",
            "productCode": "4904810000001",
        }
        ev = shadow.evaluate_catalog_candidate(listing_title, product)
        self.assertFalse(ev["g1_brand_match"])
        self.assertFalse(ev["passed_stage_r"])

    def test_g2_falls_back_to_token_match_when_product_no_missing(self):
        listing_title = "レゴ クラシック 基礎ブロック 40499 大きな缶"
        product = {
            "productName": "レゴ (LEGO) クラシック 基礎ブロック 40499",
            "productNo": "",
            "productCode": "5702016995992",
        }
        ev = shadow.evaluate_catalog_candidate(listing_title, product)
        self.assertEqual(ev["g2_method"], "token_fallback")
        self.assertTrue(ev["g2_model_match"])

    def test_g2_fails_when_product_no_not_in_listing_tokens(self):
        listing_title = "レゴ クラシック 基礎ブロック 40499 大きな缶"
        product = {
            "productName": "レゴ 別の商品",
            "productNo": "99999",
            "productCode": "5702016995993",
        }
        ev = shadow.evaluate_catalog_candidate(listing_title, product)
        self.assertFalse(ev["g2_model_match"])
        self.assertFalse(ev["passed_stage_r"])

    def test_g4_rejects_beyblade_isbn_case(self):
        # issue #3561 実測: 「ベイブレード」クエリが ISBN 9784091541376 の雑誌を返した事故。
        # G1/G2 が仮に一致しても G4 が単独で reject することを確認する。
        # (型番は _MODEL_TOKEN_RE が英数字連続のみを拾う既存仕様に合わせ、ハイフン無しの
        # "BX01" 表記にする — "BX-01" のようなハイフン入り型番はトークンが分断され G2 側の
        # 検証にならないため、ここでは G4 の単独 reject 力を見るのが目的。)
        listing_title = "タカラトミー ベイブレードX BX01 スターターセット"
        product = {
            "productName": "月刊コロコロコミック ベイブレードX 特集号 BX01",
            "productNo": "BX01",
            "makerName": "タカラトミー",
            "productCode": "9784091541376",
        }
        ev = shadow.evaluate_catalog_candidate(listing_title, product)
        self.assertTrue(ev["g1_brand_match"])
        self.assertTrue(ev["g2_model_match"])
        self.assertTrue(ev["g4_is_isbn"])
        self.assertFalse(ev["passed_stage_r"])

    def test_g4_does_not_reject_normal_jan(self):
        product = {"productCode": "5702016995992"}
        self.assertFalse(shadow._is_isbn(product["productCode"]))

    def test_g4_rejects_979_prefix_too(self):
        self.assertTrue(shadow._is_isbn("9791234567896"))

    def test_g4_ignores_non_13_digit_codes(self):
        # 12桁 UPC-A 等は ISBN 判定の対象外 (978/979 始まりでも桁数不一致なら False)。
        self.assertFalse(shadow._is_isbn("978123456789"))


class PriceWithinToleranceTest(unittest.TestCase):
    def test_within_40_percent(self):
        self.assertTrue(shadow._price_within_tolerance(1000, 1300))
        self.assertTrue(shadow._price_within_tolerance(1000, 700))

    def test_outside_40_percent(self):
        self.assertFalse(shadow._price_within_tolerance(1000, 1500))

    def test_missing_price_is_indeterminate(self):
        self.assertIsNone(shadow._price_within_tolerance(0, 1000))
        self.assertIsNone(shadow._price_within_tolerance(1000, 0))


class CollectShadowPopulationTest(unittest.TestCase):
    def test_excludes_jan_extractable_and_matched_items(self):
        items = [
            {"rank": 1, "matched_asin": "B0EXIST", "title": "既存記事化済"},
            {"rank": 2, "matched_asin": None, "title": "JAN抽出可能", "itemCaption": "4904810000001"},
            {"rank": 3, "matched_asin": None, "title": "JAN無し残党"},
        ]
        pop = shadow.collect_shadow_population(items)
        self.assertEqual([it["rank"] for it in pop], [3])

    def test_excludes_ranks_already_resolved_by_title_fuzzy(self):
        items = [
            {"rank": 3, "matched_asin": None, "title": "JAN無し残党その1"},
            {"rank": 4, "matched_asin": None, "title": "JAN無し残党その2"},
        ]
        manifest = {
            "title_fuzzy": {
                "title_fuzzy_resolved": [{"rank": 3, "asin": "B0RESOLVED"}],
            }
        }
        pop = shadow.collect_shadow_population(items, manifest)
        self.assertEqual([it["rank"] for it in pop], [4])

    def test_no_manifest_keeps_all_no_jan_items(self):
        items = [{"rank": 1, "matched_asin": None, "title": "JAN無し"}]
        pop = shadow.collect_shadow_population(items, None)
        self.assertEqual(len(pop), 1)


class FakeCreatorsAPI:
    """rr.resolve_jan_to_item が呼ぶ search_items(keywords=JAN) をモックする。"""

    def __init__(self, jan_to_item: dict):
        self.jan_to_item = jan_to_item
        self.calls = []

    def search_items(self, keywords=None, search_index="All", item_count=10,
                     item_page=1, resources=None):
        self.calls.append(keywords)
        item = self.jan_to_item.get(keywords)
        if not item:
            return {"searchResult": {"items": []}}
        return {"searchResult": {"items": [item]}}


def _amazon_item(asin, price, eans, roots=("おもちゃ",)):
    # extract_browse_nodes (fetch_amazon.py) は ancestor チェーンを遡って root を
    # 導出するので、テストデータも "root" キー直書きではなく ancestor ネストで表現する。
    nodes = [
        {"id": str(i), "displayName": "サブカテゴリ", "ancestor": {"displayName": r}}
        for i, r in enumerate(roots)
    ] if roots else []
    return {
        "asin": asin,
        "itemInfo": {"externalIds": {"eans": {"displayValues": eans}}},
        "offersV2": {"listings": [{"price": {"money": {"amount": price}}}]},
        "browseNodeInfo": {"browseNodes": nodes},
    }


class ProcessItemAndRunShadowTest(unittest.TestCase):
    def _mock_fetch(self, query_to_products):
        def _fake(app_id, access_key, query, hits=5, max_retries=1, backoff_sec=3.0):
            return query_to_products.get(query, [])
        return _fake

    def test_process_item_full_accept_path(self):
        item = {"rank": 5, "title": "レゴ(LEGO) クラシック 基礎ブロック 40499 大きな缶", "price": "3500"}
        product = {
            "productName": "レゴ (LEGO) クラシック 基礎ブロック 40499",
            "productNo": "40499",
            "makerName": "レゴジャパン",
            "productCode": "5702016995992",
        }
        queries = shadow.build_catalog_queries(item["title"])
        primary_query = queries[0][1]
        api = FakeCreatorsAPI({
            "5702016995992": _amazon_item("B0NEWJAN01", 3800, ["5702016995992"]),
        })
        with patch.object(shadow, "fetch_catalog_candidates",
                          side_effect=self._mock_fetch({primary_query: [product]})):
            record = shadow.process_item(item, "app", "key", api, sleep=0)
        self.assertTrue(record["accepted"])
        self.assertEqual(record["stage_a"]["asin"], "B0NEWJAN01")
        self.assertEqual(record["stage_a"]["jan"], "5702016995992")

    def test_process_item_rejects_when_no_candidates_pass_guardrails(self):
        item = {"rank": 6, "title": "レゴ クラシック 基礎ブロック 40499 大きな缶", "price": "3500"}
        mismatched_product = {
            "productName": "全く関係ないブランドの別商品",
            "productNo": "00000",
            "productCode": "4900000000000",
        }
        queries = shadow.build_catalog_queries(item["title"])
        primary_query = queries[0][1]
        api = FakeCreatorsAPI({})
        with patch.object(shadow, "fetch_catalog_candidates",
                          side_effect=self._mock_fetch({primary_query: [mismatched_product]})):
            record = shadow.process_item(item, "app", "key", api, sleep=0)
        self.assertFalse(record["accepted"])
        self.assertIsNone(record["selected_candidate"])
        self.assertIsNone(record["stage_a"])

    def test_process_item_rejects_on_genre_gate(self):
        item = {"rank": 7, "title": "レゴ(LEGO) クラシック 基礎ブロック 40499 大きな缶", "price": "3500"}
        product = {
            "productName": "レゴ (LEGO) クラシック 基礎ブロック 40499",
            "productNo": "40499",
            "productCode": "5702016995992",
        }
        queries = shadow.build_catalog_queries(item["title"])
        primary_query = queries[0][1]
        api = FakeCreatorsAPI({
            "5702016995992": _amazon_item("B0NONTOY01", 3800, ["5702016995992"], roots=("家電",)),
        })
        with patch.object(shadow, "fetch_catalog_candidates",
                          side_effect=self._mock_fetch({primary_query: [product]})):
            record = shadow.process_item(item, "app", "key", api, sleep=0)
        self.assertFalse(record["accepted"])
        self.assertEqual(record["stage_a"]["reason"], "genre_gate")

    def test_process_item_rejects_on_price_out_of_range(self):
        item = {"rank": 8, "title": "レゴ(LEGO) クラシック 基礎ブロック 40499 大きな缶", "price": "1000"}
        product = {
            "productName": "レゴ (LEGO) クラシック 基礎ブロック 40499",
            "productNo": "40499",
            "productCode": "5702016995992",
        }
        queries = shadow.build_catalog_queries(item["title"])
        primary_query = queries[0][1]
        api = FakeCreatorsAPI({
            # 楽天1000円に対しAmazon10000円 (±40%を大きく外れる)。
            "5702016995992": _amazon_item("B0PRICEOFF", 10000, ["5702016995992"]),
        })
        with patch.object(shadow, "fetch_catalog_candidates",
                          side_effect=self._mock_fetch({primary_query: [product]})):
            record = shadow.process_item(item, "app", "key", api, sleep=0)
        self.assertFalse(record["accepted"])
        self.assertEqual(record["stage_a"]["reason"], "price_out_of_range")

    def test_run_shadow_manifest_shape_and_limit(self):
        items = [
            {"rank": 1, "title": "レゴ クラシック 基礎ブロック 40499 大きな缶", "price": "3500"},
            {"rank": 2, "title": "無関係な商品タイトル", "price": "1000"},
        ]
        api = FakeCreatorsAPI({})
        with patch.object(shadow, "fetch_catalog_candidates", return_value=[]):
            manifest = shadow.run_shadow(items, "app", "key", api, limit=1, sleep=0)
        self.assertTrue(manifest["shadow"])
        self.assertEqual(manifest["input_population_before_limit"], 2)
        self.assertEqual(manifest["input_population"], 1)
        self.assertEqual(len(manifest["items"]), 1)
        self.assertEqual(manifest["accepted_count"], 0)
        self.assertEqual(manifest["accepted"], [])


if __name__ == "__main__":
    unittest.main()
