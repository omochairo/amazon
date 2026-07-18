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
        self.assertEqual(data["summary"], {"total_demand_queries": 0, "gap_count": 0, "by_theme": {}})
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
