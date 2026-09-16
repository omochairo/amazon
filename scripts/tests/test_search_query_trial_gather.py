"""scripts/experimental/search_query_trial/gather.py の単体テスト (#4841 V2)。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import requests

from scripts.experimental.search_query_trial import gather as G


class _FakeResponse:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    def __init__(self, by_url: dict):
        self._by_url = by_url
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        outcome = self._by_url.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Q0UrlsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_reads_third_party_sources_only(self):
        asin_dir = self.base / "B0000000AA"
        asin_dir.mkdir(parents=True)
        (asin_dir / "third_party_sources.json").write_text(json.dumps({
            "sources": [
                {"url": "https://note.com/1"},
                {"url": "https://www.google.com/search?q=x"},  # 検索結果ページ除外
            ],
        }), encoding="utf-8")
        (asin_dir / "news.json").write_text(json.dumps({"items": [
            {"url": "https://news.example.com/1"},
        ]}), encoding="utf-8")
        urls = G.q0_urls("B0000000AA", self.base)
        # news.json は対象外 (検索語試験の対象は third_party_sources.json のみ)
        self.assertEqual(urls, ["https://note.com/1"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(G.q0_urls("B0NOTFOUND1", self.base), [])


class FetchBodiesTest(unittest.TestCase):
    def test_success_and_failure_are_both_logged(self):
        session = _FakeSession({
            "https://ok.example.com/1": _FakeResponse(
                text="<html><body>レゴ クラシック の口コミです</body></html>",
            ),
            "https://fail.example.com/2": requests.ConnectionError("boom"),
        })
        sleeps = []
        candidates, log = G.fetch_bodies(
            ["https://ok.example.com/1", "https://fail.example.com/2"],
            source_type="blog", session=session, sleeper=sleeps.append,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_url"], "https://ok.example.com/1")
        self.assertEqual(candidates[0]["source_type"], "blog")
        statuses = {row["url"]: row["status"] for row in log}
        self.assertEqual(statuses["https://ok.example.com/1"], "ok")
        self.assertEqual(statuses["https://fail.example.com/2"], "fetch_failed")
        # 1秒1リクエストの礼儀 (sleeper が URL ごとに呼ばれる)
        self.assertEqual(len(sleeps), 2)

    def test_empty_body_is_logged_but_not_a_candidate(self):
        session = _FakeSession({
            "https://empty.example.com/1": _FakeResponse(text="<html><body></body></html>"),
        })
        candidates, log = G.fetch_bodies(
            ["https://empty.example.com/1"], source_type="blog",
            session=session, sleeper=lambda s: None,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(log[0]["status"], "empty_body")

    def test_http_error_status_is_failure(self):
        session = _FakeSession({
            "https://notfound.example.com/1": _FakeResponse(status=404),
        })
        candidates, log = G.fetch_bodies(
            ["https://notfound.example.com/1"], source_type="blog",
            session=session, sleeper=lambda s: None,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(log[0]["status"], "fetch_failed")


if __name__ == "__main__":
    unittest.main()
