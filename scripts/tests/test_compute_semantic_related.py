"""Unit tests for compute_semantic_related (#2995 案1)。

カバレッジ:
1. 記事選定 (discover_articles): `.quality.json` 除外・rewrite 新旧併存の
   winner 選択・不正ファイル名のスキップ
2. 埋め込みテキスト組み立て (build_embedding_text): フィールド連結・欠損耐性・
   1200 文字切り詰め
3. E2E (compute_neighbors / run, embeddings をモック): top-k 上限・min-score
   閾値・自己除外・決定的な同点順序・スコア丸め・出力 JSON 形状 (_meta 込み)・
   近傍 0 件記事の省略
4. abort 挙動: 接続エラーがリトライ後も解消しない場合に例外を送出し、出力
   ファイルを書き込まないこと
5. --limit / --dry-run の挙動
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

import compute_semantic_related as C  # noqa: E402


def _write_article(dir_path: pathlib.Path, filename: str, data: dict | None = None) -> pathlib.Path:
    p = dir_path / filename
    p.write_text(json.dumps(data if data is not None else {"title": "テスト"}, ensure_ascii=False), encoding="utf-8")
    return p


def _mock_post_response(embeddings: list[list[float]]):
    r = mock.Mock()
    r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value={"embeddings": embeddings})
    return r


class DiscoverArticlesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_excludes_quality_sidecars(self):
        _write_article(self.articles_dir, "2026-05-01-B0XXXXXXXX.json")
        (self.articles_dir / "2026-05-01-B0XXXXXXXX.quality.json").write_text("{}", encoding="utf-8")
        out = C.discover_articles(self.articles_dir)
        self.assertEqual(list(out.keys()), ["B0XXXXXXXX"])

    def test_picks_newest_stem_for_duplicate_asin(self):
        _write_article(self.articles_dir, "2026-05-01-B0YYYYYYYY.json", {"title": "old"})
        _write_article(self.articles_dir, "2026-06-01-B0YYYYYYYY.json", {"title": "new"})
        out = C.discover_articles(self.articles_dir)
        self.assertEqual(out["B0YYYYYYYY"].name, "2026-06-01-B0YYYYYYYY.json")

    def test_ignores_files_with_invalid_asin_token(self):
        _write_article(self.articles_dir, "not-an-asin-token.json")
        _write_article(self.articles_dir, "2026-05-01-short.json")
        out = C.discover_articles(self.articles_dir)
        self.assertEqual(out, {})

    def test_accepts_all_digit_asin(self):
        # 4910762175 (JAN/EAN 由来の 10 桁数字 ASIN も実データに存在する)
        _write_article(self.articles_dir, "2026-05-17-4910762175.json")
        out = C.discover_articles(self.articles_dir)
        self.assertEqual(list(out.keys()), ["4910762175"])

    def test_missing_dir_returns_empty(self):
        out = C.discover_articles(self.articles_dir / "does-not-exist")
        self.assertEqual(out, {})


class BuildEmbeddingTextTest(unittest.TestCase):
    def test_concatenates_all_fields_with_newlines(self):
        article = {
            "title": "タイトル",
            "product": {"name": "商品名", "edu_domains": ["数の理解", "色彩感覚"]},
            "tags": ["タグA", "タグB"],
            "keywords": ["キーワードA", "キーワードB"],
            "narrative": {"lead": "リード文"},
            "persona_fit": {"age_range": "3歳以上"},
        }
        text = C.build_embedding_text(article)
        lines = text.split("\n")
        self.assertEqual(
            lines,
            [
                "タイトル",
                "商品名",
                "タグ: タグA, タグB",
                "キーワード: キーワードA, キーワードB",
                "リード文",
                "対象年齢: 3歳以上",
                "知育領域: 数の理解, 色彩感覚",
            ],
        )

    def test_missing_fields_do_not_crash_and_are_skipped(self):
        text = C.build_embedding_text({})
        self.assertEqual(text, "")

    def test_partial_fields_only_included_ones_appear(self):
        article = {"title": "タイトルのみ", "product": {}}
        text = C.build_embedding_text(article)
        self.assertEqual(text, "タイトルのみ")

    def test_non_dict_article_returns_empty(self):
        self.assertEqual(C.build_embedding_text(None), "")  # type: ignore[arg-type]
        self.assertEqual(C.build_embedding_text([1, 2, 3]), "")  # type: ignore[arg-type]

    def test_truncates_to_1200_chars(self):
        article = {"title": "あ" * 2000}
        text = C.build_embedding_text(article)
        self.assertEqual(len(text), 1200)
        self.assertEqual(text, "あ" * 1200)

    def test_empty_string_fields_skipped(self):
        article = {"title": "  ", "product": {"name": ""}, "narrative": {"lead": "本文"}}
        text = C.build_embedding_text(article)
        self.assertEqual(text, "本文")


class CosineSimilarityTest(unittest.TestCase):
    def test_pure_python_fallback_matches_expected_values(self):
        vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
        sim = C._cosine_similarity_pure(vectors)
        self.assertAlmostEqual(sim[0][0], 1.0, places=6)
        self.assertAlmostEqual(sim[0][1], 0.0, places=6)
        self.assertAlmostEqual(sim[0][2], 0.7071067811865476, places=6)
        self.assertAlmostEqual(sim[1][2], 0.7071067811865476, places=6)

    def test_zero_vector_does_not_crash(self):
        vectors = [[0.0, 0.0], [1.0, 0.0]]
        sim = C._cosine_similarity_pure(vectors)
        self.assertEqual(sim[0][0], 0.0)
        self.assertEqual(sim[0][1], 0.0)

    def test_compute_similarity_matrix_forces_pure_python_fallback_when_numpy_missing(self):
        vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
        # sys.modules に None を仕込むと import 文が ImportError を送出する
        # (numpy が実際にインストールされていても、この with 内では強制的に
        # フォールバック経路を通す)。
        with mock.patch.dict(sys.modules, {"numpy": None}):
            sim_fallback = C.compute_similarity_matrix(vectors)
        sim_direct = C._cosine_similarity_pure(vectors)
        self.assertEqual(sim_fallback, sim_direct)

    def test_compute_similarity_matrix_matches_pure_python_when_numpy_available(self):
        vectors = [[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [0.5, 0.5, 0.5]]
        sim_numpy = C.compute_similarity_matrix(vectors)
        sim_pure = C._cosine_similarity_pure(vectors)
        for row_a, row_b in zip(sim_numpy, sim_pure):
            for a, b in zip(row_a, row_b):
                self.assertAlmostEqual(a, b, places=9)

    def test_empty_input_returns_empty(self):
        self.assertEqual(C.compute_similarity_matrix([]), [])


class ComputeNeighborsTest(unittest.TestCase):
    def test_top_k_min_score_self_exclusion_tie_order_and_rounding(self):
        asins = ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC", "DDDDDDDDDD"]
        # A の近傍候補: B=0.9001 (丸めで0.9), C=0.7005 (丸めで0.701って伝わるよう調整),
        # D=0.59 (閾値未満で除外)。B と C は同点になるよう別テストで扱う。
        similarity = [
            [1.0, 0.9001, 0.7005, 0.59],
            [0.9001, 1.0, 0.65, 0.61],
            [0.7005, 0.65, 1.0, 0.60],
            [0.59, 0.61, 0.60, 1.0],
        ]
        result = C.compute_neighbors(asins, similarity, top_k=8, min_score=0.60)
        self.assertIn("aaaaaaaaaa", result)
        neighbors_a = result["aaaaaaaaaa"]
        # D (score 0.59 < 0.60) は除外、自己 (A) も含まれない
        neighbor_asins = [n["asin"] for n in neighbors_a]
        self.assertNotIn("aaaaaaaaaa", neighbor_asins)
        self.assertNotIn("dddddddddd", neighbor_asins)
        # score 降順: B (0.9001->0.9) が先頭
        self.assertEqual(neighbor_asins[0], "bbbbbbbbbb")
        self.assertEqual(neighbors_a[0]["score"], 0.9)
        self.assertEqual(neighbors_a[1]["score"], round(0.7005, 3))

    def test_deterministic_tie_break_by_asin_ascending(self):
        asins = ["ZZZZZZZZZZ", "AAAAAAAAAA", "MMMMMMMMMM"]
        # 全員 0.8 で同点 -> asin 昇順で並ぶはず
        similarity = [
            [1.0, 0.8, 0.8],
            [0.8, 1.0, 0.8],
            [0.8, 0.8, 1.0],
        ]
        result = C.compute_neighbors(asins, similarity, top_k=8, min_score=0.5)
        neighbor_asins = [n["asin"] for n in result["zzzzzzzzzz"]]
        self.assertEqual(neighbor_asins, sorted(neighbor_asins))

    def test_top_k_caps_result_count(self):
        n = 5
        asins = [f"A{str(i).zfill(9)}" for i in range(n)]
        similarity = [[1.0 if i == j else 0.9 for j in range(n)] for i in range(n)]
        result = C.compute_neighbors(asins, similarity, top_k=2, min_score=0.5)
        for asin_key in result:
            self.assertLessEqual(len(result[asin_key]), 2)

    def test_zero_neighbors_above_threshold_are_omitted(self):
        asins = ["AAAAAAAAAA", "BBBBBBBBBB"]
        similarity = [[1.0, 0.1], [0.1, 1.0]]
        result = C.compute_neighbors(asins, similarity, top_k=8, min_score=0.6)
        self.assertEqual(result, {})


class RunEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "semantic_related.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_three_articles(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json", {"title": "商品C"})

    def test_output_shape_and_meta(self):
        self._write_three_articles()
        # A と B が似ていて (0.95)、C はどちらとも遠い (0.1) ベクトルを用意する。
        # 単純な2次元的直交構成: A=[1,0], B=[0.99, 0.14] (cos~0.99), C=[0,1]
        embeddings = [[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]]
        session = mock.Mock()
        session.post.return_value = _mock_post_response(embeddings)

        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
            ollama_url="http://fake-ollama:11434", model="fake-model",
            session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["written"])
        self.assertTrue(self.out_path.exists())
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertIn("_meta", data)
        self.assertEqual(data["_meta"]["model"], "fake-model")
        self.assertEqual(data["_meta"]["articles"], 3)
        self.assertIn("generated_at", data["_meta"])
        # A <-> B は近傍として現れるはず (lowercase asin keys)
        self.assertIn("b0aaaaaaaa", data)
        neighbor_asins_a = [n["asin"] for n in data["b0aaaaaaaa"]]
        self.assertIn("b0bbbbbbbb", neighbor_asins_a)
        self.assertNotIn("b0aaaaaaaa", neighbor_asins_a)
        # スコアは丸められた float
        for n in data["b0aaaaaaaa"]:
            self.assertEqual(round(n["score"], 3), n["score"])

    def test_request_payload_uses_model_and_batches(self):
        self._write_three_articles()
        embeddings_batch1 = [[1.0, 0.0], [0.0, 1.0]]
        embeddings_batch2 = [[0.5, 0.5]]
        session = mock.Mock()
        session.post.side_effect = [
            _mock_post_response(embeddings_batch1),
            _mock_post_response(embeddings_batch2),
        ]
        C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.0, batch_size=2, limit=0, dry_run=False,
            ollama_url="http://fake-ollama:11434", model="my-model",
            session=session, sleeper=lambda _s: None,
        )
        self.assertEqual(session.post.call_count, 2)
        first_call_args, first_call_kwargs = session.post.call_args_list[0]
        self.assertEqual(first_call_kwargs["json"]["model"], "my-model")
        self.assertEqual(len(first_call_kwargs["json"]["input"]), 2)
        self.assertTrue(first_call_args[0].endswith("/api/embed"))


def _mock_ruri_response(vectors: list[list[float]]):
    r = mock.Mock()
    r.raise_for_status = mock.Mock()
    r.json = mock.Mock(return_value={"dim": len(vectors[0]) if vectors else 0, "vectors": vectors})
    return r


class RunRuriBackendTest(unittest.TestCase):
    """#2995 案1 K8 LLM ワーカー経路 (backend="ruri") のリクエスト形状・E2E テスト。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "semantic_related.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_three_articles(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json", {"title": "商品C"})

    def test_ruri_request_payload_and_url(self):
        self._write_three_articles()
        embeddings_batch1 = [[1.0, 0.0], [0.0, 1.0]]
        embeddings_batch2 = [[0.5, 0.5]]
        session = mock.Mock()
        session.post.side_effect = [
            _mock_ruri_response(embeddings_batch1),
            _mock_ruri_response(embeddings_batch2),
        ]
        C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.0, batch_size=2, limit=0, dry_run=False,
            backend="ruri", ruri_url="http://fake-ruri:8000", model="cl-nagoya/ruri-v3-310m",
            session=session, sleeper=lambda _s: None,
        )
        self.assertEqual(session.post.call_count, 2)
        first_call_args, first_call_kwargs = session.post.call_args_list[0]
        self.assertEqual(first_call_kwargs["json"]["kind"], "document")
        self.assertEqual(len(first_call_kwargs["json"]["texts"]), 2)
        self.assertNotIn("model", first_call_kwargs["json"])
        self.assertTrue(first_call_args[0].endswith("/embed"))
        self.assertFalse(first_call_args[0].endswith("/api/embed"))

    def test_ruri_end_to_end_output_shape_and_meta(self):
        self._write_three_articles()
        embeddings = [[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]]
        session = mock.Mock()
        session.post.return_value = _mock_ruri_response(embeddings)

        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
            backend="ruri", ruri_url="http://fake-ruri:8000", model="cl-nagoya/ruri-v3-310m",
            session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["written"])
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["_meta"]["model"], "cl-nagoya/ruri-v3-310m")
        neighbor_asins_a = [n["asin"] for n in data["b0aaaaaaaa"]]
        self.assertIn("b0bbbbbbbb", neighbor_asins_a)

    def test_ruri_connection_error_retries_then_raises_and_writes_nothing(self):
        self._write_three_articles()
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(C.EmbeddingBatchError):
            C.run(
                self.articles_dir, self.out_path,
                top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
                backend="ruri", ruri_url="http://fake-ruri:8000",
                session=session, sleeper=lambda _s: None,
            )
        self.assertEqual(session.post.call_count, 3)
        self.assertFalse(self.out_path.exists())

    def test_unknown_backend_raises_value_error(self):
        self._write_three_articles()
        session = mock.Mock()
        with self.assertRaises(ValueError):
            C.run(
                self.articles_dir, self.out_path,
                backend="unknown-backend", session=session, sleeper=lambda _s: None,
            )


class RunAbortOnEmbeddingFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "semantic_related.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_connection_error_retries_then_raises_and_writes_nothing(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", {"title": "商品A"})
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json", {"title": "商品B"})

        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(C.EmbeddingBatchError):
            C.run(
                self.articles_dir, self.out_path,
                top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
                ollama_url="http://fake-ollama:11434", model="fake-model",
                session=session, sleeper=lambda _s: None,
            )
        # 初回 + 2 リトライ = 3 回試行しているはず
        self.assertEqual(session.post.call_count, 3)
        self.assertFalse(self.out_path.exists())

    def test_http_error_status_retries_then_raises(self):
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json", {"title": "商品C"})

        bad_resp = mock.Mock()
        bad_resp.raise_for_status.side_effect = requests.HTTPError("500 server error")
        session = mock.Mock()
        session.post.return_value = bad_resp

        with self.assertRaises(C.EmbeddingBatchError):
            C.run(
                self.articles_dir, self.out_path,
                top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
                session=session, sleeper=lambda _s: None,
            )
        self.assertEqual(session.post.call_count, 3)
        self.assertFalse(self.out_path.exists())

    def test_unexpected_response_shape_retries_then_raises(self):
        _write_article(self.articles_dir, "2026-05-01-B0DDDDDDDD.json", {"title": "商品D"})
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json = mock.Mock(return_value={"embeddings": []})  # 件数不一致
        session = mock.Mock()
        session.post.return_value = resp

        with self.assertRaises(C.EmbeddingBatchError):
            C.run(
                self.articles_dir, self.out_path,
                top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
                session=session, sleeper=lambda _s: None,
            )
        self.assertFalse(self.out_path.exists())


class RunLimitAndDryRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.articles_dir.mkdir()
        self.out_path = self.root / "out" / "semantic_related.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_n_articles(self, n: int):
        for i in range(n):
            _write_article(
                self.articles_dir, f"2026-05-01-B0{str(i).zfill(8)}.json", {"title": f"商品{i}"}
            )

    def test_limit_caps_articles_processed(self):
        self._write_n_articles(5)
        session = mock.Mock()
        session.post.return_value = _mock_post_response([[1.0, 0.0], [0.0, 1.0]])
        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=2, dry_run=False,
            session=session, sleeper=lambda _s: None,
        )
        self.assertEqual(summary["articles"], 2)
        # embed_batch には 2 件分のテキストしか渡らない
        sent_texts = session.post.call_args.kwargs["json"]["input"]
        self.assertEqual(len(sent_texts), 2)

    def test_limit_zero_means_all(self):
        self._write_n_articles(3)
        session = mock.Mock()
        session.post.return_value = _mock_post_response([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
            session=session, sleeper=lambda _s: None,
        )
        self.assertEqual(summary["articles"], 3)

    def test_dry_run_computes_but_does_not_write(self):
        self._write_n_articles(2)
        session = mock.Mock()
        session.post.return_value = _mock_post_response([[1.0, 0.0], [0.0, 1.0]])
        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=True,
            session=session, sleeper=lambda _s: None,
        )
        self.assertFalse(summary["written"])
        self.assertFalse(self.out_path.exists())
        # dry-run でも embed 計算自体は行われる (compute but do not write)
        session.post.assert_called()

    def test_no_articles_found_does_not_call_network_or_write(self):
        session = mock.Mock()
        summary = C.run(
            self.articles_dir, self.out_path,
            top_k=8, min_score=0.6, batch_size=32, limit=0, dry_run=False,
            session=session, sleeper=lambda _s: None,
        )
        self.assertEqual(summary["articles"], 0)
        self.assertFalse(summary["written"])
        session.post.assert_not_called()
        self.assertFalse(self.out_path.exists())


if __name__ == "__main__":
    unittest.main()
