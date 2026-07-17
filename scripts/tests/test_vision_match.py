"""vision_match.py の単体テスト (Issue #3332 N5-V1)。

実サービス (K8 画像埋め込みエンドポイント) は未デプロイなのでネットワーク呼び出しは
一切行わない。``ImageEmbeddingClient.embed_images`` はモック HTTP セッションで検証し、
``image_similarity`` / ``passes_vision_gate`` はモッククライアント (DI) で検証する。
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import vision_match as vm  # noqa: E402


class CosineSimilarityTest(unittest.TestCase):
    def test_identical_vectors_score_one(self):
        self.assertAlmostEqual(vm.cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_orthogonal_vectors_score_zero(self):
        self.assertAlmostEqual(vm.cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_opposite_vectors_score_negative_one(self):
        self.assertAlmostEqual(vm.cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_mismatched_length_returns_zero(self):
        self.assertEqual(vm.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)

    def test_empty_vectors_return_zero(self):
        self.assertEqual(vm.cosine_similarity([], []), 0.0)

    def test_zero_vector_returns_zero(self):
        # 0除算回避: ゼロベクトルとの類似度は未定義なので 0.0 に丸める。
        self.assertEqual(vm.cosine_similarity([0.0, 0.0], [1.0, 0.0]), 0.0)


class FakeVisionClient:
    """URL → ベクトルの固定マップを返すモック (DI テスト用)。"""

    def __init__(self, url_to_vector: dict):
        self.url_to_vector = url_to_vector
        self.calls: list = []

    def embed_images(self, image_urls):
        self.calls.append(list(image_urls))
        return [self.url_to_vector.get(u) for u in image_urls]


class ImageSimilarityTest(unittest.TestCase):
    def test_returns_cosine_of_embeddings(self):
        client = FakeVisionClient({
            "https://rakuten/a.jpg": [1.0, 0.0],
            "https://amazon/b.jpg": [1.0, 0.0],
        })
        score = vm.image_similarity(client, "https://rakuten/a.jpg", "https://amazon/b.jpg")
        self.assertAlmostEqual(score, 1.0)
        self.assertEqual(client.calls, [["https://rakuten/a.jpg", "https://amazon/b.jpg"]])

    def test_dissimilar_images_low_score(self):
        client = FakeVisionClient({
            "https://rakuten/a.jpg": [1.0, 0.0],
            "https://amazon/b.jpg": [0.0, 1.0],
        })
        score = vm.image_similarity(client, "https://rakuten/a.jpg", "https://amazon/b.jpg")
        self.assertAlmostEqual(score, 0.0)

    def test_empty_urls_return_none(self):
        client = FakeVisionClient({})
        self.assertIsNone(vm.image_similarity(client, "", "https://amazon/b.jpg"))
        self.assertIsNone(vm.image_similarity(client, "https://rakuten/a.jpg", ""))

    def test_missing_embedding_returns_none(self):
        # 片方の URL の埋め込みが取得できない (None) 場合は未評価として None を返す。
        client = FakeVisionClient({"https://rakuten/a.jpg": [1.0, 0.0]})
        score = vm.image_similarity(client, "https://rakuten/a.jpg", "https://amazon/missing.jpg")
        self.assertIsNone(score)

    def test_client_exception_returns_none_fail_closed(self):
        class BoomClient:
            def embed_images(self, urls):
                raise RuntimeError("service unavailable")

        score = vm.image_similarity(BoomClient(), "https://rakuten/a.jpg", "https://amazon/b.jpg")
        self.assertIsNone(score)

    def test_unexpected_vector_count_returns_none(self):
        class WrongShapeClient:
            def embed_images(self, urls):
                return [[1.0, 0.0]]  # 1件しか返さない (期待は2件)

        score = vm.image_similarity(WrongShapeClient(), "https://rakuten/a.jpg", "https://amazon/b.jpg")
        self.assertIsNone(score)


class PassesVisionGateTest(unittest.TestCase):
    def test_score_above_threshold_passes(self):
        self.assertTrue(vm.passes_vision_gate(0.95, 0.9))

    def test_score_equal_threshold_passes(self):
        self.assertTrue(vm.passes_vision_gate(0.9, 0.9))

    def test_score_below_threshold_fails(self):
        self.assertFalse(vm.passes_vision_gate(0.5, 0.9))

    def test_none_score_fails_closed(self):
        self.assertFalse(vm.passes_vision_gate(None, 0.9))


class ImageEmbeddingClientTest(unittest.TestCase):
    """HTTP レイヤ自体はモック ``requests.Session`` 相当のスタブで検証する。"""

    def test_embed_images_empty_list_returns_empty_without_request(self):
        class NoCallSession:
            def post(self, *a, **kw):
                raise AssertionError("should not be called for empty input")

        client = vm.ImageEmbeddingClient("http://vision.example", session=NoCallSession())
        self.assertEqual(client.embed_images([]), [])

    def test_embed_images_success(self):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"vectors": [[0.1, 0.2], [0.3, 0.4]]}

        class FakeSession:
            def __init__(self):
                self.posted_urls = []

            def post(self, url, json=None, timeout=None):
                self.posted_urls.append(url)
                return FakeResponse()

        session = FakeSession()
        client = vm.ImageEmbeddingClient("http://vision.example/", session=session)
        vectors = client.embed_images(["u1", "u2"])
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(session.posted_urls, ["http://vision.example/embed_image"])

    def test_embed_images_retries_then_raises_on_persistent_failure(self):
        calls = {"n": 0}

        class AlwaysFailSession:
            def post(self, *a, **kw):
                calls["n"] += 1
                raise vm.requests.exceptions.ConnectionError("down")

        sleeps = []
        client = vm.ImageEmbeddingClient(
            "http://vision.example", session=AlwaysFailSession(),
            sleeper=lambda s: sleeps.append(s),
        )
        with self.assertRaises(vm.VisionEmbeddingError):
            client.embed_images(["u1", "u2"])
        # _MAX_EXTRA_RETRIES=2 なので計 3 回呼ばれる (初回 + リトライ2回)。
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_embed_images_wrong_shape_raises(self):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"vectors": [[0.1, 0.2]]}  # 1件のみ (期待は2件)

        class FakeSession:
            def post(self, *a, **kw):
                return FakeResponse()

        client = vm.ImageEmbeddingClient(
            "http://vision.example", session=FakeSession(), sleeper=lambda s: None
        )
        with self.assertRaises(vm.VisionEmbeddingError):
            client.embed_images(["u1", "u2"])


if __name__ == "__main__":
    unittest.main()
