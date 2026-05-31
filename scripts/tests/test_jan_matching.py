"""JAN/EAN マッチング機能 (#660) のユニットテスト。

カバレッジ:
1. fetch_amazon.extract_jan が PA-API itemInfo.externalIds.eans から JAN を抽出する
2. fetch_cross_search.search_rakuten_tiered が jan_code 指定時に
   Rakuten Books `isbnjan` を最優先で叩く
3. fetch_cross_search._yahoo_query が jan_code 指定時に
   `jan_code` パラメータで Yahoo API を呼び、`query` は送らない
4. JAN なし (空文字) のとき従来テキスト検索フローに自動フォールバックする
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# scripts/ を import path に追加
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_amazon  # noqa: E402
import fetch_cross_search  # noqa: E402


class ExtractJanTest(unittest.TestCase):
    def test_extract_jan_from_externalids(self):
        item = {
            "itemInfo": {
                "externalIds": {
                    "eans": {"displayValues": ["4904790012345"]}
                }
            }
        }
        self.assertEqual(fetch_amazon.extract_jan(item), "4904790012345")

    def test_extract_jan_empty_when_missing(self):
        self.assertEqual(fetch_amazon.extract_jan({}), "")
        self.assertEqual(fetch_amazon.extract_jan({"itemInfo": {}}), "")
        self.assertEqual(
            fetch_amazon.extract_jan({"itemInfo": {"externalIds": {"eans": {"displayValues": []}}}}),
            "",
        )

    def test_extract_jan_strips_whitespace(self):
        item = {"itemInfo": {"externalIds": {"eans": {"displayValues": ["  4900000000001  "]}}}}
        self.assertEqual(fetch_amazon.extract_jan(item), "4900000000001")


class RakutenJanFlowTest(unittest.TestCase):
    def _mk_resp(self, status, payload):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = payload
        m.text = ""
        return m

    def test_jan_books_isbnjan_hit_returns_immediately(self):
        """JAN ヒット時は Books `isbnjan` の最初の hit を即返却し、
        Ichiba やテキスト Stage は一切呼ばないこと。"""
        books_payload = {
            "Items": [
                {"itemName": "テスト商品A", "itemPrice": 1500, "itemUrl": "http://example.com/a",
                 "mediumImageUrls": ["http://img/a.jpg"], "availability": 1},
            ]
        }
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            mock_get.return_value = self._mk_resp(200, books_payload)
            result = fetch_cross_search.search_rakuten_tiered(
                "テスト キーワード", app_id="dummy", jan_code="4900000000001"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "jan_books")
        self.assertEqual(result["source"], "Rakuten Books")
        # 1 回しか呼ばれていない (Stage 0a で確定)
        self.assertEqual(mock_get.call_count, 1)
        sent_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent_params.get("isbnjan"), "4900000000001")
        self.assertNotIn("keyword", sent_params)

    def test_jan_ichiba_fallback_when_books_empty(self):
        """Books が 0 hit のとき Ichiba JAN keyword に落ち、そこでヒットしたら
        テキスト Stage には進まない。"""
        empty = {"Items": []}
        ichiba_hit = {
            "Items": [
                {"itemName": "テスト商品B", "itemPrice": 2000, "itemUrl": "http://example.com/b",
                 "mediumImageUrls": ["http://img/b.jpg"], "availability": 1},
            ]
        }
        responses = [self._mk_resp(200, empty), self._mk_resp(200, ichiba_hit)]
        with patch.object(fetch_cross_search.requests, "get", side_effect=responses) as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_rakuten_tiered(
                "テスト キーワード", app_id="dummy", jan_code="4900000000002"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "jan_ichiba")
        self.assertEqual(mock_get.call_count, 2)
        # 2 回目は Ichiba で keyword=JAN になっている
        ichiba_call_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(ichiba_call_params.get("keyword"), "4900000000002")

    def test_falls_back_to_text_when_jan_unavailable(self):
        """jan_code が空のとき Books 経由のテキスト検索が動き、
        `isbnjan` パラメータは送られない。"""
        books_payload = {
            "Items": [
                {"itemName": "テキストヒット", "itemPrice": 1200, "itemUrl": "http://example.com/c",
                 "mediumImageUrls": ["http://img/c.jpg"], "availability": 1},
            ]
        }
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            mock_get.return_value = self._mk_resp(200, books_payload)
            result = fetch_cross_search.search_rakuten_tiered(
                "ブランド キーワード", app_id="dummy", jan_code=""
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "text")
        self.assertEqual(mock_get.call_count, 1)
        sent_params = mock_get.call_args.kwargs["params"]
        self.assertNotIn("isbnjan", sent_params)
        self.assertEqual(sent_params.get("keyword"), "ブランド キーワード")


class YahooJanFlowTest(unittest.TestCase):
    def _mk_yahoo_resp(self, hits):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"hits": hits}
        return m

    def test_yahoo_query_uses_jan_code_param(self):
        """jan_code 指定時は params に `jan_code` を入れ、`query` は送らない。
        さらに `in_stock=true` フィルタは text 検索のみ。JAN は unique ID なので
        在庫切れ商品も valid (PR fix/yahoo-jan-search-loosen-in-stock)。"""
        hits = [{"name": "Yテスト", "price": 1800, "url": "http://y.example/x",
                 "image": {"medium": "http://img/y.jpg"}, "inStock": True,
                 "shipping": {"code": 2}}]
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            mock_get.return_value = self._mk_yahoo_resp(hits)
            items = fetch_cross_search._yahoo_query("", "dummy_appid", jan_code="4900000000003")
        self.assertEqual(len(items), 1)
        sent_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent_params.get("jan_code"), "4900000000003")
        self.assertNotIn("query", sent_params)
        # ★ in_stock フィルタは JAN 検索では掛けない (regression guard)
        self.assertNotIn("in_stock", sent_params)

    def test_yahoo_query_text_search_keeps_in_stock_filter(self):
        """text 検索 (jan_code 無し) は従来通り `in_stock=true` を付与する。"""
        hits = [{"name": "Yテキスト", "price": 1500, "url": "http://y.example/t",
                 "image": {"medium": "http://img/yt.jpg"}, "inStock": True,
                 "shipping": {"code": 2}}]
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            mock_get.return_value = self._mk_yahoo_resp(hits)
            fetch_cross_search._yahoo_query("ブランド ワード", "dummy_appid")
        sent_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent_params.get("query"), "ブランド ワード")
        self.assertEqual(sent_params.get("in_stock"), "true")
        self.assertNotIn("jan_code", sent_params)

    def test_search_yahoo_jan_first_then_text(self):
        """search_yahoo は jan_code があれば最初に JAN クエリを叩き、
        ヒットすれば後続のテキスト候補は叩かない。_match_method=jan が立つ。"""
        jan_hits = [{"name": "Y-JAN-hit", "price": 2500, "url": "http://y.example/j",
                     "image": {"medium": "http://img/yj.jpg"}, "inStock": True,
                     "shipping": {"code": 2}}]
        with patch.object(fetch_cross_search.requests, "get") as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            mock_get.return_value = self._mk_yahoo_resp(jan_hits)
            result = fetch_cross_search.search_yahoo(
                "ブランド ワード", "dummy_appid", jan_code="4900000000004"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "jan")
        # JAN クエリ 1 回のみ (テキスト stage に進まず)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_get.call_args.kwargs["params"].get("jan_code"), "4900000000004")

    def test_search_yahoo_fallback_to_text_when_jan_miss(self):
        """JAN がヒットしないとき (空 hits) テキストフローに落ちる。"""
        empty_hits = self._mk_yahoo_resp([])
        text_hits = self._mk_yahoo_resp([
            {"name": "Y-text-hit", "price": 1900, "url": "http://y.example/t",
             "image": {"medium": "http://img/yt.jpg"}, "inStock": True,
             "shipping": {"code": 2}},
        ])
        with patch.object(fetch_cross_search.requests, "get", side_effect=[empty_hits, text_hits]) as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_yahoo(
                "ブランド ワード", "dummy_appid", jan_code="4900000000005"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "text")
        self.assertEqual(mock_get.call_count, 2)
        # 1 回目は JAN クエリ、2 回目は query テキスト
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"].get("jan_code"), "4900000000005")
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"].get("query"), "ブランド ワード")


class JanAttemptObservabilityTest(unittest.TestCase):
    """Issue #1087 Phase 1: `_jan_attempt` field が JAN フェーズ outcome を
    正しく記録することを確認する。"""

    def _mk_resp(self, status, payload, text=""):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = payload
        m.text = text
        return m

    def test_rakuten_jan_books_hit_records_jan_books(self):
        books_payload = {"Items": [{"itemName": "A", "itemPrice": 1000,
                                     "itemUrl": "http://x/a", "availability": 1}]}
        with patch.object(fetch_cross_search.requests, "get") as mock_get:
            mock_get.return_value = self._mk_resp(200, books_payload)
            result = fetch_cross_search.search_rakuten_tiered(
                "kw", app_id="x", jan_code="4900000000010"
            )
        self.assertEqual(result["_jan_attempt"], "jan_books")

    def test_rakuten_jan_zero_falls_to_text_records_jan_zero(self):
        """0a と 0b が共に 0 hits → text fallback で hit したら _jan_attempt=jan_zero。"""
        empty = self._mk_resp(200, {"Items": []})
        text_hit = self._mk_resp(200, {"Items": [
            {"itemName": "T", "itemPrice": 1500, "itemUrl": "http://x/t", "availability": 1}
        ]})
        # Stage 0a (zero) → 0b (zero) → Stage 1 Books text (hit)
        with patch.object(fetch_cross_search.requests, "get",
                          side_effect=[empty, empty, text_hit]) as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_rakuten_tiered(
                "ブランド ワード", app_id="x", jan_code="4900000000011"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_match_method"], "text")
        self.assertEqual(result["_jan_attempt"], "jan_zero")
        self.assertEqual(mock_get.call_count, 3)

    def test_rakuten_jan_400_classified(self):
        """Stage 0a HTTP 400 → 0b HTTP 400 → text hit で _jan_attempt=jan_400。"""
        err400 = self._mk_resp(400, {}, text="keyword is not valid")
        # Stage 2 Ichiba text hit (Books Stage 1 は空にする)
        empty = self._mk_resp(200, {"Items": []})
        ichiba_hit = self._mk_resp(200, {"Items": [
            {"itemName": "I", "itemPrice": 2000, "itemUrl": "http://x/i", "availability": 1}
        ]})
        with patch.object(fetch_cross_search.requests, "get",
                          side_effect=[err400, err400, empty, ichiba_hit]) as mock_get, \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_rakuten_tiered(
                "ブランド ワード", app_id="x", jan_code="4900000000012"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["_jan_attempt"], "jan_400")

    def test_rakuten_no_jan_records_no_jan(self):
        text_hit = self._mk_resp(200, {"Items": [
            {"itemName": "N", "itemPrice": 1000, "itemUrl": "http://x/n", "availability": 1}
        ]})
        with patch.object(fetch_cross_search.requests, "get", return_value=text_hit), \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_rakuten_tiered("kw", app_id="x", jan_code="")
        self.assertEqual(result["_jan_attempt"], "no_jan")

    def test_yahoo_jan_hit_records_jan(self):
        hit = MagicMock()
        hit.status_code = 200
        hit.json.return_value = {"hits": [
            {"name": "Y", "price": 2500, "url": "http://y/j",
             "image": {"medium": "http://i"}, "inStock": True, "shipping": {"code": 2}}
        ]}
        with patch.object(fetch_cross_search.requests, "get", return_value=hit), \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_yahoo(
                "kw", "appid", jan_code="4900000000020"
            )
        self.assertEqual(result["_jan_attempt"], "jan")

    def test_yahoo_jan_zero_falls_to_text_records_jan_zero(self):
        empty = MagicMock(); empty.status_code = 200; empty.json.return_value = {"hits": []}
        text = MagicMock(); text.status_code = 200; text.json.return_value = {"hits": [
            {"name": "T", "price": 1500, "url": "http://y/t",
             "image": {"medium": "http://i"}, "inStock": True, "shipping": {"code": 2}}
        ]}
        with patch.object(fetch_cross_search.requests, "get",
                          side_effect=[empty, text]), \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_yahoo(
                "kw", "appid", jan_code="4900000000021"
            )
        self.assertEqual(result["_match_method"], "text")
        self.assertEqual(result["_jan_attempt"], "jan_zero")

    def test_yahoo_jan_http_5xx_classified(self):
        err = MagicMock(); err.status_code = 503; err.json.return_value = {}; err.text = ""
        text = MagicMock(); text.status_code = 200; text.json.return_value = {"hits": [
            {"name": "T", "price": 1500, "url": "http://y/t",
             "image": {"medium": "http://i"}, "inStock": True, "shipping": {"code": 2}}
        ]}
        with patch.object(fetch_cross_search.requests, "get",
                          side_effect=[err, text]), \
             patch.object(fetch_cross_search.time, "sleep"):
            result = fetch_cross_search.search_yahoo(
                "kw", "appid", jan_code="4900000000022"
            )
        self.assertEqual(result["_jan_attempt"], "jan_5xx")


class CollectTargetsJanTest(unittest.TestCase):
    def test_collect_targets_passes_jan_from_amazon_json(self):
        amazon_items = [
            {"asin": "B000TEST01", "title": "テスト商品1", "jan_code": "4904790000001"},
            {"asin": "B000TEST02", "title": "テスト商品2"},  # JAN 欠損
        ]
        # articles_dir 不在 + per_asin_root 不在で純粋に amazon.json 由来のみ確認
        import pathlib
        empty = pathlib.Path("/__nonexistent_path_for_test__")
        targets = fetch_cross_search._collect_targets(amazon_items, empty, empty)
        self.assertEqual(targets["B000TEST01"], ("テスト商品1", True, "4904790000001"))
        self.assertEqual(targets["B000TEST02"], ("テスト商品2", True, ""))


if __name__ == "__main__":
    unittest.main()
