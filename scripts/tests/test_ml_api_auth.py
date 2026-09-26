"""ruri / vision API へ X-API-Key を載せる経路のテスト (amazon-home-ops#158)。

RURI_API_TOKEN / VISION_API_TOKEN が設定されていれば全呼び出し箇所がヘッダを送り、
未設定なら従来どおりヘッダを付けない (サービス側も未設定なら無認証で受ける)。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts import (
    audit_experience_usage,
    build_wp_navi_link_candidates,
    build_wp_wp_h2_link_candidates,
    compute_semantic_related,
    detect_demand_gaps,
    ml_api_auth,
    vision_match,
)


def _session(payload):
    session = mock.MagicMock()
    resp = mock.MagicMock()
    resp.json.return_value = payload
    session.post.return_value = resp
    return session


def _sent_headers(session):
    return session.post.call_args.kwargs.get("headers")


_EMBED_OK = {"vectors": [[0.1, 0.2]], "dim": 2}


class HelperTest(unittest.TestCase):
    def test_unset_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ml_api_auth.ruri_headers(), {})
            self.assertEqual(ml_api_auth.vision_headers(), {})

    def test_blank_is_treated_as_unset(self):
        with mock.patch.dict(os.environ, {"RURI_API_TOKEN": "  "}, clear=True):
            self.assertEqual(ml_api_auth.ruri_headers(), {})

    def test_tokens_are_independent(self):
        env = {"RURI_API_TOKEN": "r-tok", "VISION_API_TOKEN": "v-tok"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ml_api_auth.ruri_headers(), {"X-API-Key": "r-tok"})
            self.assertEqual(ml_api_auth.vision_headers(), {"X-API-Key": "v-tok"})


class CallSitesSendHeaderTest(unittest.TestCase):
    """全呼び出し箇所 (/embed・/rerank・/embed_image) がヘッダを送ること。"""

    def setUp(self):
        patcher = mock.patch.dict(
            os.environ, {"RURI_API_TOKEN": "r-tok", "VISION_API_TOKEN": "v-tok"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_compute_semantic_related_embed(self):
        s = _session(_EMBED_OK)
        compute_semantic_related.embed_batch_ruri(["t"], "http://ruri", s, sleeper=lambda _: None)
        self.assertEqual(_sent_headers(s), {"X-API-Key": "r-tok"})

    def test_detect_demand_gaps_query_embed(self):
        s = _session(_EMBED_OK)
        detect_demand_gaps.embed_batch_ruri_query(["t"], "http://ruri", s, sleeper=lambda _: None)
        self.assertEqual(_sent_headers(s), {"X-API-Key": "r-tok"})

    def test_link_candidates_and_experience_usage_embed_and_rerank(self):
        for mod in (build_wp_navi_link_candidates, build_wp_wp_h2_link_candidates, audit_experience_usage):
            with self.subTest(mod=mod.__name__):
                s = _session(_EMBED_OK)
                mod.embed_batch_ruri(["t"], "query", "http://ruri", s, sleeper=lambda _: None)
                self.assertEqual(_sent_headers(s), {"X-API-Key": "r-tok"})
        for mod in (build_wp_navi_link_candidates, build_wp_wp_h2_link_candidates):
            with self.subTest(mod=mod.__name__, endpoint="rerank"):
                s = _session({"results": [{"index": 0, "score": 1.0}]})
                mod.rerank_candidates("q", ["d"], "http://ruri", s, sleeper=lambda _: None)
                self.assertTrue(s.post.call_args.args[0].endswith("/rerank"))
                self.assertEqual(_sent_headers(s), {"X-API-Key": "r-tok"})

    def test_vision_embed_image(self):
        s = _session(_EMBED_OK)
        vision_match.ImageEmbeddingClient("http://vision", session=s).embed_images(["u"])
        self.assertEqual(_sent_headers(s), {"X-API-Key": "v-tok"})

    def test_unset_sends_no_header(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = _session(_EMBED_OK)
            compute_semantic_related.embed_batch_ruri(["t"], "http://ruri", s, sleeper=lambda _: None)
            self.assertEqual(_sent_headers(s), {})


if __name__ == "__main__":
    unittest.main()
