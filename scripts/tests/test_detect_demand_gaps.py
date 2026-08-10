"""Unit tests for detect_demand_gaps (#3332 N2 消費側 PR1)。

カバレッジ:
1. 需要クエリ構築: suggest + gsc のマージ・正規化 dedupe・impressions 合算・
   min_impressions フィルタ
2. max_sim 計算 (既知ベクトルで cosine 検算、numpy/pure 両経路)
3. gap_threshold 境界 (未満=gap / 以上=非gap)
4. ランク順 (suggest 出現優先 → impressions 降順 → query 昇順) と by_theme 集計
5. 空入力 (需要クエリ0 / 記事0) の graceful 終了 (Ruri を呼ばない)
6. Ruri 失敗時の fail-closed (出力ファイルを書かない)
7. embed_document_fn / embed_query_fn の DI が両方使われること (kind の出し分け)
8. is_code_like_query: ASCII のみ (ASIN/型番) 判定・日本語混在は非該当
9. コード系クエリが gap 判定の母集団から除外され、summary.excluded_code_queries
   に記録されること
10. theme_key=="english" のクエリが gap 判定の母集団から除外され、
    summary.excluded_english_queries に記録されること (age 系/None テーマは対象外)
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import detect_demand_gaps as D  # noqa: E402
import compute_semantic_related as C  # noqa: E402


def _write_json(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_suggest_theme(dir_path: pathlib.Path, theme_key: str, queries: list[str]) -> None:
    _write_json(dir_path / f"{theme_key}.json", {
        "theme_key": theme_key,
        "theme_label": theme_key,
        "theme_kind": "age",
        "lane": "A",
        "seed_type": "informational",
        "seeds": [],
        "suggestions": [{"query": q, "seed": "seed", "source": "google"} for q in queries],
        "fetched_at": "2026-07-01T00:00:00Z",
    })


def _write_gsc_lines(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _write_article(dir_path: pathlib.Path, filename: str, data: dict) -> None:
    (dir_path / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# 需要クエリ構築
# --------------------------------------------------------------------------

class BuildDemandQueriesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.suggest_dir = self.root / "suggest_info"
        self.suggest_dir.mkdir()
        self.gsc_path = self.root / "gsc_by_query.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_suggest_only_query_has_zero_impressions_and_theme_key(self):
        _write_suggest_theme(self.suggest_dir, "age-0", ["0歳 おもちゃ 知育"])
        _write_gsc_lines(self.gsc_path, [])
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        self.assertEqual(len(out), 1)
        q = out[0]
        self.assertEqual(q["query"], "0歳 おもちゃ 知育")
        self.assertEqual(q["sources"], ["suggest"])
        self.assertEqual(q["impressions"], 0)
        self.assertEqual(q["theme_key"], "age-0")

    def test_gsc_impressions_aggregated_across_dates_and_filtered_by_min_impressions(self):
        _write_gsc_lines(self.gsc_path, [
            {"query": "英語 おもちゃ", "impressions": 2, "date": "2026-06-01"},
            {"query": "英語 おもちゃ", "impressions": 2, "date": "2026-06-02"},
            {"query": "算数 おもちゃ", "impressions": 1, "date": "2026-06-01"},
        ])
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        queries = {q["query"]: q for q in out}
        # 英語 おもちゃ: 2+2=4 >= 3 なので採用
        self.assertIn("英語 おもちゃ", queries)
        self.assertEqual(queries["英語 おもちゃ"]["impressions"], 4)
        self.assertEqual(queries["英語 おもちゃ"]["sources"], ["gsc"])
        self.assertIsNone(queries["英語 おもちゃ"]["theme_key"])
        # 算数 おもちゃ: 1 < 3 なので除外
        self.assertNotIn("算数 おもちゃ", queries)

    def test_query_appearing_in_both_sources_merges_with_both_sources(self):
        _write_suggest_theme(self.suggest_dir, "age-1", ["プログラミング おもちゃ"])
        _write_gsc_lines(self.gsc_path, [
            {"query": "プログラミング おもちゃ", "impressions": 5, "date": "2026-06-01"},
        ])
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        self.assertEqual(len(out), 1)
        q = out[0]
        self.assertEqual(set(q["sources"]), {"suggest", "gsc"})
        self.assertEqual(q["impressions"], 5)
        self.assertEqual(q["theme_key"], "age-1")

    def test_normalization_dedupes_full_width_space_and_case(self):
        _write_suggest_theme(self.suggest_dir, "age-2", ["ABC　おもちゃ"])  # 全角スペース
        _write_gsc_lines(self.gsc_path, [
            {"query": "abc おもちゃ", "impressions": 10, "date": "2026-06-01"},  # 半角+小文字
        ])
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]["sources"]), {"suggest", "gsc"})

    def test_malformed_gsc_lines_are_skipped_without_crashing(self):
        self.gsc_path.write_text(
            "not json\n" + json.dumps({"query": "有効", "impressions": 5}) + "\n{}\n",
            encoding="utf-8",
        )
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["query"], "有効")

    def test_missing_files_return_empty(self):
        out = D.build_demand_queries(
            self.suggest_dir / "does-not-exist", self.gsc_path / "missing.jsonl", min_impressions=3
        )
        self.assertEqual(out, [])

    # ---- WP (omcha.jp) 需要ソース (2026-08-10 追加) ----

    def test_wp_impressions_kept_in_separate_field_not_summed(self):
        """別サイトの数字なので合算しない (意味が違う)。"""
        wp = self.root / "gsc_wp_by_query.jsonl"
        _write_gsc_lines(self.gsc_path, [{"query": "メルちゃん 服", "impressions": 4}])
        _write_gsc_lines(wp, [{"query": "メルちゃん 服", "impressions": 14863}])
        out = D.build_demand_queries(
            self.suggest_dir, self.gsc_path, min_impressions=3,
            gsc_wp_query_path=wp, min_wp_impressions=50,
        )
        self.assertEqual(len(out), 1)
        q = out[0]
        self.assertEqual(q["impressions"], 4)
        self.assertEqual(q["wp_impressions"], 14863)
        self.assertEqual(set(q["sources"]), {"gsc", "gsc_wp"})

    def test_wp_only_query_is_picked_up(self):
        """navi に実績ゼロでも WP に需要があれば拾う (これが導入の目的)。"""
        wp = self.root / "gsc_wp_by_query.jsonl"
        _write_gsc_lines(self.gsc_path, [])
        _write_gsc_lines(wp, [{"query": "ぷにるんず", "impressions": 6930}])
        out = D.build_demand_queries(
            self.suggest_dir, self.gsc_path, min_impressions=3,
            gsc_wp_query_path=wp, min_wp_impressions=50,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sources"], ["gsc_wp"])
        self.assertEqual(out[0]["impressions"], 0)
        self.assertEqual(out[0]["wp_impressions"], 6930)

    def test_wp_min_impressions_threshold_is_independent(self):
        wp = self.root / "gsc_wp_by_query.jsonl"
        _write_gsc_lines(self.gsc_path, [])
        _write_gsc_lines(wp, [
            {"query": "採用される", "impressions": 50},
            {"query": "裾のノイズ", "impressions": 49},
        ])
        out = D.build_demand_queries(
            self.suggest_dir, self.gsc_path, min_impressions=3,
            gsc_wp_query_path=wp, min_wp_impressions=50,
        )
        self.assertEqual([q["query"] for q in out], ["採用される"])

    def test_wp_source_disabled_reproduces_previous_behaviour(self):
        """--no-wp-demand 相当 (gsc_wp_query_path=None) で旧挙動に戻ること。"""
        _write_gsc_lines(self.gsc_path, [{"query": "従来クエリ", "impressions": 5}])
        out = D.build_demand_queries(self.suggest_dir, self.gsc_path, min_impressions=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sources"], ["gsc"])
        self.assertEqual(out[0]["wp_impressions"], 0)


class DemandRankKeyTest(unittest.TestCase):
    """需要シグナルの順位付け (2026-08-10 に WP impressions を一次キーへ)。"""

    def test_wp_impressions_outrank_suggest_presence(self):
        big_wp = {"query": "メルちゃん 服", "sources": ["gsc_wp"], "impressions": 0,
                  "wp_impressions": 14863}
        suggest_only = {"query": "0歳 おもちゃ", "sources": ["suggest"], "impressions": 0,
                        "wp_impressions": 0}
        self.assertEqual(
            sorted([suggest_only, big_wp], key=D._demand_rank_key)[0]["query"], "メルちゃん 服")

    def test_suggest_still_outranks_navi_impressions_when_wp_ties(self):
        suggest_only = {"query": "b", "sources": ["suggest"], "impressions": 0, "wp_impressions": 0}
        navi_only = {"query": "a", "sources": ["gsc"], "impressions": 999, "wp_impressions": 0}
        self.assertEqual(sorted([navi_only, suggest_only], key=D._demand_rank_key)[0]["query"], "b")

    def test_missing_wp_field_is_treated_as_zero(self):
        """旧フォーマットのレコード (wp_impressions なし) でも落ちないこと。"""
        legacy = {"query": "旧", "sources": ["gsc"], "impressions": 10}
        self.assertEqual(D._demand_rank_key(legacy)[0], 0)


# --------------------------------------------------------------------------
# コード系クエリ (ASIN/型番) 判定
# --------------------------------------------------------------------------

class IsCodeLikeQueryTest(unittest.TestCase):
    def test_ascii_alnum_query_is_code_like(self):
        self.assertTrue(D.is_code_like_query("b0h1b5qdjk"))

    def test_ascii_digits_query_is_code_like(self):
        self.assertTrue(D.is_code_like_query("75445"))

    def test_ascii_with_hyphen_query_is_code_like(self):
        self.assertTrue(D.is_code_like_query("hnw46-986q"))

    def test_japanese_query_is_not_code_like(self):
        self.assertFalse(D.is_code_like_query("0歳 おもちゃ 知育"))

    def test_mixed_ascii_and_japanese_is_not_code_like(self):
        self.assertFalse(D.is_code_like_query("レゴ 75445"))

    def test_empty_or_whitespace_only_is_not_code_like(self):
        self.assertFalse(D.is_code_like_query(""))
        self.assertFalse(D.is_code_like_query("   "))
        self.assertFalse(D.is_code_like_query(None))

    def test_ascii_query_with_surrounding_whitespace_is_code_like(self):
        self.assertTrue(D.is_code_like_query("  b0aaaaaaaa  "))


# --------------------------------------------------------------------------
# 最大コサイン類似度
# --------------------------------------------------------------------------

class ComputeMaxSimilarityTest(unittest.TestCase):
    def test_pure_python_finds_nearest_doc(self):
        query_vectors = [[1.0, 0.0], [0.0, 1.0]]
        doc_vectors = [[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]]
        results = D._max_similarity_pure(query_vectors, doc_vectors)
        self.assertEqual(results[0][0], 0)  # query0 は doc0 に最も近い
        self.assertAlmostEqual(results[0][1], 1.0, places=6)
        self.assertEqual(results[1][0], 2)  # query1 は doc2 に最も近い
        self.assertAlmostEqual(results[1][1], 1.0, places=6)

    def test_numpy_matches_pure_python(self):
        query_vectors = [[1.0, 2.0, 3.0], [3.0, 1.0, 0.0]]
        doc_vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
        pure = D._max_similarity_pure(query_vectors, doc_vectors)
        numpy_result = D._max_similarity_numpy(query_vectors, doc_vectors)
        for (pi, ps), (ni, ns) in zip(pure, numpy_result):
            self.assertEqual(pi, ni)
            self.assertAlmostEqual(ps, ns, places=9)

    def test_compute_max_similarity_falls_back_without_numpy(self):
        query_vectors = [[1.0, 0.0]]
        doc_vectors = [[1.0, 0.0], [0.0, 1.0]]
        with mock.patch.dict(sys.modules, {"numpy": None}):
            fallback = D.compute_max_similarity(query_vectors, doc_vectors)
        direct = D._max_similarity_pure(query_vectors, doc_vectors)
        self.assertEqual(fallback, direct)

    def test_empty_doc_vectors_returns_sentinel(self):
        result = D.compute_max_similarity([[1.0, 0.0]], [])
        self.assertEqual(result, [(-1, 0.0)])

    def test_empty_query_vectors_returns_empty_list(self):
        result = D.compute_max_similarity([], [[1.0, 0.0]])
        self.assertEqual(result, [])

    def test_zero_vector_does_not_crash(self):
        result = D._max_similarity_pure([[0.0, 0.0]], [[1.0, 0.0]])
        self.assertEqual(result[0][1], 0.0)


# --------------------------------------------------------------------------
# E2E run() (embed_document_fn / embed_query_fn を DI でモック)
# --------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.suggest_dir = self.root / "suggest_info"
        self.suggest_dir.mkdir()
        self.gsc_path = self.root / "gsc_by_query.jsonl"
        self.out_path = self.root / "out" / "demand_gaps.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_embed_fns(self, doc_vectors, query_vectors):
        """kind の出し分けを検証しつつ固定ベクトルを返す DI 関数ペアを作る。"""
        doc_calls = []
        query_calls = []

        def embed_document_fn(texts, ruri_url, session, sleeper):
            doc_calls.append(list(texts))
            return doc_vectors

        def embed_query_fn(texts, ruri_url, session, sleeper):
            query_calls.append(list(texts))
            return query_vectors

        return embed_document_fn, embed_query_fn, doc_calls, query_calls

    def test_gap_threshold_boundary_and_nearest_article_fields(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["近いクエリ", "遠いクエリ"])
        _write_gsc_lines(self.gsc_path, [])

        # doc0(=B0AAAAAAAA)=[1,0], doc1(=B0BBBBBBBB)=[0,1]
        doc_vectors = [[1.0, 0.0], [0.0, 1.0]]
        # query0="近いクエリ": doc0 と cos=1.0 (>= 0.5 threshold -> gap ではない)
        # query1="遠いクエリ": どちらとも直交 cos=0 (< 0.5 threshold -> gap)
        query_vectors = [[1.0, 0.0], [0.0, 0.0]]
        embed_document_fn, embed_query_fn, doc_calls, query_calls = self._fake_embed_fns(
            doc_vectors, query_vectors
        )

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            gap_threshold=0.5, min_impressions=3, limit_articles=0, dry_run=False,
            session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 2)
        self.assertEqual(summary["gap_count"], 1)
        self.assertTrue(summary["written"])

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_demand_queries"], 2)
        self.assertEqual(data["summary"]["gap_count"], 1)
        self.assertEqual(len(data["gaps"]), 1)
        gap = data["gaps"][0]
        self.assertEqual(gap["query"], "遠いクエリ")
        self.assertEqual(gap["max_sim"], 0.0)
        self.assertIn(gap["nearest_asin"], ("B0AAAAAAAA", "B0BBBBBBBB"))
        self.assertIn(gap["nearest_title"], ("商品A", "商品B"))
        self.assertEqual(data["summary"]["by_theme"], {"age-0": 1})

        # document 側は kind 分岐なし (呼ばれるテキストは記事の埋め込みテキスト)
        self.assertEqual(len(doc_calls[0]), 2)
        # query 側は 2 件の需要クエリを渡している
        self.assertEqual(len(query_calls[0]), 2)

    def test_rank_order_suggest_priority_then_impressions_desc(self):
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json", {"title": "商品C"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["suggest専用クエリ"])
        _write_gsc_lines(self.gsc_path, [
            {"query": "gsc専用クエリ高imp", "impressions": 100, "date": "2026-06-01"},
            {"query": "gsc専用クエリ低imp", "impressions": 5, "date": "2026-06-01"},
        ])
        # 全クエリが記事と無関係 (直交) になるよう doc=[1,0], query群=[0,1] で統一
        doc_vectors = [[1.0, 0.0]]
        query_vectors = [[0.0, 1.0], [0.0, 1.0], [0.0, 1.0]]
        embed_document_fn, embed_query_fn, _, _ = self._fake_embed_fns(doc_vectors, query_vectors)

        D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            gap_threshold=0.5, min_impressions=3, limit_articles=0, dry_run=False,
            session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        queries_in_order = [g["query"] for g in data["gaps"]]
        # suggest 出現 (suggest専用クエリ) が最優先、次に impressions 降順
        self.assertEqual(
            queries_in_order,
            ["suggest専用クエリ", "gsc専用クエリ高imp", "gsc専用クエリ低imp"],
        )

    def test_code_like_queries_excluded_from_population_and_counted_in_summary(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        # suggest 側にコード系 (ASIN 風) + 自然文を混在させる
        _write_suggest_theme(self.suggest_dir, "age-0", ["b0h1b5qdjk", "遠いクエリ"])
        _write_gsc_lines(self.gsc_path, [
            {"query": "75445", "impressions": 10, "date": "2026-06-01"},
        ])

        # 記事=[1,0]。自然文クエリ "遠いクエリ" のみ [0,0] で直交 (gap)。
        # embed_query_fn は除外後に渡された順序 (["遠いクエリ"]) 分だけ返す。
        def embed_document_fn(texts, ruri_url, session, sleeper):
            return [[1.0, 0.0] for _ in texts]

        captured_query_texts = []

        def embed_query_fn(texts, ruri_url, session, sleeper):
            captured_query_texts.append(list(texts))
            return [[0.0, 0.0] for _ in texts]

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            gap_threshold=0.5, min_impressions=3, dry_run=False,
            session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )

        # コード系2件 (b0h1b5qdjk, 75445) が母集団から除外され、残るのは自然文1件のみ
        self.assertEqual(summary["total_demand_queries"], 1)
        self.assertEqual(captured_query_texts, [["遠いクエリ"]])

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_demand_queries"], 1)
        self.assertEqual(data["summary"]["excluded_code_queries"], 2)
        self.assertEqual(data["summary"]["gap_count"], 1)
        self.assertEqual(data["gaps"][0]["query"], "遠いクエリ")

    def test_all_code_like_queries_yields_empty_report_without_calling_ruri(self):
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["b0h1b5qdjk", "75445"])
        embed_document_fn = mock.Mock()
        embed_query_fn = mock.Mock()

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            dry_run=False, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 0)
        embed_document_fn.assert_not_called()
        embed_query_fn.assert_not_called()

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["excluded_code_queries"], 2)
        self.assertEqual(data["summary"]["total_demand_queries"], 0)

    def test_english_theme_queries_excluded_from_population_and_counted_in_summary(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        # english テーマ (翻訳クエリ) + 自然文の age テーマを混在させる
        _write_suggest_theme(self.suggest_dir, "age-0", ["遠いクエリ"])
        _write_suggest_theme(
            self.suggest_dir, "english", ["おもちゃ 英語", "おもちゃ が 壊れ た 英語"]
        )

        def embed_document_fn(texts, ruri_url, session, sleeper):
            return [[1.0, 0.0] for _ in texts]

        captured_query_texts = []

        def embed_query_fn(texts, ruri_url, session, sleeper):
            captured_query_texts.append(list(texts))
            return [[0.0, 0.0] for _ in texts]

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            gap_threshold=0.5, min_impressions=3, dry_run=False,
            session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )

        # english テーマ2件が母集団から除外され、残るのは age-0 の自然文1件のみ
        self.assertEqual(summary["total_demand_queries"], 1)
        self.assertEqual(captured_query_texts, [["遠いクエリ"]])

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_demand_queries"], 1)
        self.assertEqual(data["summary"]["excluded_code_queries"], 0)
        self.assertEqual(data["summary"]["excluded_english_queries"], 2)
        self.assertEqual(data["summary"]["gap_count"], 1)
        self.assertEqual(data["gaps"][0]["query"], "遠いクエリ")

    def test_all_english_theme_queries_yields_empty_report_without_calling_ruri(self):
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})
        _write_suggest_theme(
            self.suggest_dir, "english", ["おもちゃ 英語", "おもちゃ が 壊れ た 英語"]
        )
        embed_document_fn = mock.Mock()
        embed_query_fn = mock.Mock()

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            dry_run=False, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 0)
        embed_document_fn.assert_not_called()
        embed_query_fn.assert_not_called()

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["excluded_english_queries"], 2)
        self.assertEqual(data["summary"]["total_demand_queries"], 0)

    def test_non_english_theme_queries_are_not_excluded(self):
        # age 系テーマや gsc 単独 (theme_key=None) のクエリは english 除外の対象外
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json", {"title": "商品C"})
        _write_suggest_theme(self.suggest_dir, "age-1", ["age系クエリ"])
        _write_gsc_lines(self.gsc_path, [
            {"query": "gsc単独クエリ", "impressions": 10, "date": "2026-06-01"},
        ])
        embed_document_fn, embed_query_fn, _, query_calls = self._fake_embed_fns(
            [[1.0, 0.0]], [[0.0, 0.0], [0.0, 0.0]]
        )

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            gap_threshold=0.5, min_impressions=3, dry_run=False,
            session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 2)
        self.assertEqual(set(query_calls[0]), {"age系クエリ", "gsc単独クエリ"})

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["excluded_english_queries"], 0)
        self.assertEqual(data["summary"]["total_demand_queries"], 2)

    def test_empty_demand_queries_skips_ruri_and_writes_empty_report(self):
        _write_article(self.articles_dir, "2026-05-01-B0DDDDDDDD.json", {"title": "商品D"})
        # suggest_info も gsc も空
        embed_document_fn = mock.Mock()
        embed_query_fn = mock.Mock()

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            dry_run=False, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 0)
        self.assertEqual(summary["gap_count"], 0)
        self.assertTrue(summary["written"])
        embed_document_fn.assert_not_called()
        embed_query_fn.assert_not_called()

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["summary"],
            {
                "total_demand_queries": 0,
                "gap_count": 0,
                "by_theme": {},
                "excluded_code_queries": 0,
                "excluded_english_queries": 0,
            },
        )
        self.assertEqual(data["gaps"], [])

    def test_empty_articles_skips_ruri_and_writes_empty_report(self):
        # 記事コーパスが空だが需要クエリはある
        _write_suggest_theme(self.suggest_dir, "age-0", ["クエリのみ存在"])
        embed_document_fn = mock.Mock()
        embed_query_fn = mock.Mock()

        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            dry_run=False, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(summary["total_demand_queries"], 1)
        self.assertEqual(summary["gap_count"], 0)
        self.assertTrue(summary["written"])
        embed_document_fn.assert_not_called()
        embed_query_fn.assert_not_called()

        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["gaps"], [])

    def test_dry_run_computes_but_does_not_write(self):
        _write_article(self.articles_dir, "2026-05-01-B0EEEEEEEE.json", {"title": "商品E"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["dry run クエリ"])
        embed_document_fn, embed_query_fn, _, _ = self._fake_embed_fns(
            [[1.0, 0.0]], [[0.0, 1.0]]
        )
        summary = D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            dry_run=True, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertFalse(summary["written"])
        self.assertFalse(self.out_path.exists())

    def test_limit_articles_caps_corpus_size(self):
        for i in range(5):
            _write_article(
                self.articles_dir, f"2026-05-01-B0{str(i).zfill(8)}.json", {"title": f"商品{i}"}
            )
        _write_suggest_theme(self.suggest_dir, "age-0", ["制限テスト"])

        captured = {}

        def embed_document_fn(texts, ruri_url, session, sleeper):
            captured["n_docs"] = len(texts)
            return [[1.0, 0.0] for _ in texts]

        def embed_query_fn(texts, ruri_url, session, sleeper):
            return [[0.0, 1.0] for _ in texts]

        D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            limit_articles=2, dry_run=False, session=mock.Mock(), sleeper=lambda _s: None,
            embed_document_fn=embed_document_fn, embed_query_fn=embed_query_fn,
        )
        self.assertEqual(captured["n_docs"], 2)


# --------------------------------------------------------------------------
# Ruri fail-closed (実の embed_batch_ruri / embed_batch_ruri_query 経路, session をモック)
# --------------------------------------------------------------------------

class EmbedChunkedTest(unittest.TestCase):
    """_embed_chunked: 低レベル embed fn (1呼び出し=1リクエスト) へのバッチ分割。

    全件を単一リクエストに載せると記事 1,500 件超で REQUEST_TIMEOUT を必ず
    超過する (2026-07-19 初回 shadow run で実測) ことへの回帰テスト。
    """

    def test_splits_into_batches_and_preserves_order(self):
        calls: list[int] = []

        def fake_embed(texts, ruri_url, session, sleeper):
            calls.append(len(texts))
            return [[float(hash(t) % 100)] for t in texts]

        texts = [f"t{i}" for i in range(70)]
        vectors = D._embed_chunked(
            texts, fake_embed, "http://ruri:8000", None, None, batch_size=32
        )
        self.assertEqual(calls, [32, 32, 6])
        self.assertEqual(len(vectors), 70)
        # 順序保存: 各テキストに対応するベクトルが同じ位置に来る
        self.assertEqual(vectors[0], [float(hash("t0") % 100)])
        self.assertEqual(vectors[69], [float(hash("t69") % 100)])

    def test_default_batch_size_is_compute_semantic_related_default(self):
        calls: list[int] = []

        def fake_embed(texts, ruri_url, session, sleeper):
            calls.append(len(texts))
            return [[0.0]] * len(texts)

        n = C.DEFAULT_BATCH_SIZE + 1
        D._embed_chunked([f"t{i}" for i in range(n)], fake_embed, "u", None, None)
        self.assertEqual(calls, [C.DEFAULT_BATCH_SIZE, 1])

    def test_empty_input_no_calls(self):
        fn = mock.Mock()
        self.assertEqual(D._embed_chunked([], fn, "u", None, None), [])
        fn.assert_not_called()


class RuriFailClosedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.suggest_dir = self.root / "suggest_info"
        self.suggest_dir.mkdir()
        self.gsc_path = self.root / "gsc_by_query.jsonl"
        self.out_path = self.root / "out" / "demand_gaps.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_document_embed_connection_error_raises_and_writes_nothing(self):
        _write_article(self.articles_dir, "2026-05-01-B0FFFFFFFF.json", {"title": "商品F"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["失敗するクエリ"])

        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(C.EmbeddingBatchError):
            D.run(
                self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
                ruri_url="http://fake-ruri:8000", session=session, sleeper=lambda _s: None,
            )
        self.assertFalse(self.out_path.exists())

    def test_query_embed_failure_after_document_success_raises_and_writes_nothing(self):
        _write_article(self.articles_dir, "2026-05-01-B0GGGGGGGG.json", {"title": "商品G"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["2番目に失敗するクエリ"])

        doc_resp = mock.Mock()
        doc_resp.raise_for_status = mock.Mock()
        doc_resp.json = mock.Mock(return_value={"vectors": [[1.0, 0.0]]})

        session = mock.Mock()
        # 1回目 (document 側) は成功、2回目以降 (query 側、リトライ込み) は失敗
        session.post.side_effect = [doc_resp, requests.ConnectionError("boom")] + [
            requests.ConnectionError("boom")
        ] * 5

        with self.assertRaises(C.EmbeddingBatchError):
            D.run(
                self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
                ruri_url="http://fake-ruri:8000", session=session, sleeper=lambda _s: None,
            )
        self.assertFalse(self.out_path.exists())

    def test_default_embed_query_fn_sends_kind_query(self):
        _write_article(self.articles_dir, "2026-05-01-B0HHHHHHHH.json", {"title": "商品H"})
        _write_suggest_theme(self.suggest_dir, "age-0", ["kind確認クエリ"])

        doc_resp = mock.Mock()
        doc_resp.raise_for_status = mock.Mock()
        doc_resp.json = mock.Mock(return_value={"vectors": [[1.0, 0.0]]})
        query_resp = mock.Mock()
        query_resp.raise_for_status = mock.Mock()
        query_resp.json = mock.Mock(return_value={"vectors": [[0.0, 1.0]]})

        session = mock.Mock()
        session.post.side_effect = [doc_resp, query_resp]

        D.run(
            self.articles_dir, self.suggest_dir, self.gsc_path, self.out_path,
            ruri_url="http://fake-ruri:8000", session=session, sleeper=lambda _s: None,
        )
        first_call_kwargs = session.post.call_args_list[0].kwargs
        second_call_kwargs = session.post.call_args_list[1].kwargs
        self.assertEqual(first_call_kwargs["json"]["kind"], "document")
        self.assertEqual(second_call_kwargs["json"]["kind"], "query")


if __name__ == "__main__":
    unittest.main()
