"""internal_links.get_related_articles のユニットテスト (Issue #6103)。

omcha.jp の関連記事 API を `iro/v1` から `iro/v2` に移した際の回帰防止。
v1 と v2 はスコアの尺度が違う (v1 = 上限なしの素の合計 / v2 = 0..100 正規化)
ため、「閾値をサーバへ渡していること」「過剰取得をしていないこと」を固定する。
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

import internal_links  # noqa: E402


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _item(score, n=1, thumb="https://omcha.jp/t.jpg"):
    return {
        "id": n,
        "title": f"記事{n}",
        "url": f"https://omcha.jp/p{n}/",
        "score": score,
        "thumbnail": thumb,
    }


class DefaultsTest(unittest.TestCase):
    def test_default_base_is_v2(self):
        self.assertTrue(internal_links.DEFAULT_BASE.endswith("/iro/v2"))

    def test_default_min_score_is_v2_scale(self):
        # 0..100 スケール。v1 の 20 をそのまま持ち込んでいないこと。
        self.assertEqual(internal_links.DEFAULT_MIN_SCORE, 12)


class RequestShapeTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("OMCHA_API_BASE", None)
        os.environ.pop("OMCHA_API_KEY", None)

    def tearDown(self):
        self.env.stop()

    def test_min_score_sent_to_server_and_no_overfetch(self):
        body = {"results": [_item(50, i) for i in range(3)]}
        with patch.object(internal_links.requests, "get", return_value=_FakeResp(body)) as g:
            internal_links.get_related_articles("知育玩具 3歳 ブロック", count=3)
        url, kwargs = g.call_args[0][0], g.call_args[1]
        self.assertEqual(url, "https://omcha.jp/wp-json/iro/v2/related")
        params = kwargs["params"]
        # 過剰取得 (旧 v1 の count*2) を復活させない
        self.assertEqual(params["count"], 3)
        self.assertEqual(params["min_score"], internal_links.DEFAULT_MIN_SCORE)
        self.assertEqual(params["keyword"], "知育玩具 3歳 ブロック")
        self.assertNotIn("api_key", params)

    def test_explicit_min_score_overrides_default(self):
        with patch.object(internal_links.requests, "get",
                          return_value=_FakeResp({"results": []})) as g:
            internal_links.get_related_articles("木製 知育", count=5, min_score=30)
        params = g.call_args[1]["params"]
        self.assertEqual(params["min_score"], 30)
        self.assertEqual(params["count"], 5)

    def test_api_key_added_when_env_set(self):
        os.environ["OMCHA_API_KEY"] = "k1"
        with patch.object(internal_links.requests, "get",
                          return_value=_FakeResp({"results": []})) as g:
            internal_links.get_related_articles("木製")
        self.assertEqual(g.call_args[1]["params"]["api_key"], "k1")

    def test_base_url_env_override(self):
        os.environ["OMCHA_API_BASE"] = "https://omcha.jp/wp-json/iro/v1"
        with patch.object(internal_links.requests, "get",
                          return_value=_FakeResp({"results": []})) as g:
            internal_links.get_related_articles("木製")
        self.assertEqual(g.call_args[0][0], "https://omcha.jp/wp-json/iro/v1/related")


class ResponseHandlingTest(unittest.TestCase):
    def test_client_filter_still_enforces_min_score(self):
        # サーバが約束を破っても契約 (>= min_score) は守る
        body = {"results": [_item(5, 1), _item(40, 2)]}
        with patch.object(internal_links.requests, "get", return_value=_FakeResp(body)):
            out = internal_links.get_related_articles("木製", count=3, min_score=12)
        self.assertEqual([r["score"] for r in out], [40])

    def test_truncates_to_count(self):
        body = {"results": [_item(40, i) for i in range(10)]}
        with patch.object(internal_links.requests, "get", return_value=_FakeResp(body)):
            out = internal_links.get_related_articles("木製", count=3)
        self.assertEqual(len(out), 3)

    def test_thumbnail_passthrough_and_absence(self):
        body = {"results": [_item(40, 1), _item(40, 2, thumb=None)]}
        with patch.object(internal_links.requests, "get", return_value=_FakeResp(body)):
            out = internal_links.get_related_articles("木製", count=3)
        self.assertEqual(out[0]["thumbnail"], "https://omcha.jp/t.jpg")
        self.assertIsNone(out[1]["thumbnail"])

    def test_v2_extra_fields_do_not_leak_into_cards(self):
        # v2 は matched_keywords / modified を返すが、カードのスキーマは
        # build_post が読む 4 キーのまま固定する。
        r = _item(40, 1)
        r["matched_keywords"] = ["知育玩具"]
        r["modified"] = "2026-01-01 00:00:00"
        with patch.object(internal_links.requests, "get",
                          return_value=_FakeResp({"results": [r]})):
            out = internal_links.get_related_articles("木製", count=3)
        self.assertEqual(set(out[0]), {"title", "url", "score", "thumbnail"})

    def test_empty_keyword_short_circuits(self):
        with patch.object(internal_links.requests, "get") as g:
            self.assertEqual(internal_links.get_related_articles("   "), [])
        g.assert_not_called()

    def test_request_failure_returns_empty(self):
        with patch.object(internal_links.requests, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(internal_links.get_related_articles("木製"), [])

    def test_malformed_body_returns_empty(self):
        for body in ({"results": "nope"}, [], {"total": 0}):
            with patch.object(internal_links.requests, "get", return_value=_FakeResp(body)):
                self.assertEqual(internal_links.get_related_articles("木製"), [])


if __name__ == "__main__":
    unittest.main()
