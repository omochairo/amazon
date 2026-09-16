"""scripts/experimental/search_query_trial/query_groups.py の単体テスト (#4841 V2)。"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts.experimental.search_query_trial import query_groups as Q


def _urlopen_response(payload: dict):
    import io
    resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
    resp.__enter__ = lambda *a: resp  # type: ignore[attr-defined]
    resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
    return resp


class BuildQueryTest(unittest.TestCase):
    def test_q1_appends_fixed_phrase(self):
        self.assertEqual(Q.build_query("Q1", "レゴ クラシック"), "レゴ クラシック 使ってみた 感想")

    def test_q2_keyword_only(self):
        self.assertEqual(Q.build_query("Q2", "レゴ クラシック"), "レゴ クラシック")


class TavilySearchWithDomainsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_include_domains_added_to_request_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _urlopen_response({"results": []})

        with mock.patch.object(Q.urllib.request, "urlopen", side_effect=fake_urlopen):
            Q.tavily_search("x", "tvly-test", base=self.base, include_domains=["note.com", "ameblo.jp"])
        self.assertEqual(captured["body"]["include_domains"], ["note.com", "ameblo.jp"])

    def test_no_include_domains_key_when_not_passed(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _urlopen_response({"results": []})

        with mock.patch.object(Q.urllib.request, "urlopen", side_effect=fake_urlopen):
            Q.tavily_search("x", "tvly-test", base=self.base)
        self.assertNotIn("include_domains", captured["body"])

    def test_results_normalized_and_filterable(self):
        payload = {"results": [
            {"url": "https://note.com/1", "title": "T", "content": "C"},
        ]}
        with mock.patch.object(Q.urllib.request, "urlopen", return_value=_urlopen_response(payload)):
            items = Q.tavily_search("x", "tvly-test", base=self.base)
        out = Q._filter_sources(items, max_sources=5)
        self.assertEqual([s["host"] for s in out], ["note.com"])

    def test_records_call_to_shared_ledger_before_request(self):
        """owner修正1: Tavily を叩く直前に共有台帳へ1回ぶん刻む。"""
        def fake_urlopen(req, timeout=None):
            usage = json.loads((self.base / "_tavily_usage.json").read_text(encoding="utf-8"))
            self.assertEqual(usage["calls"], 1)
            return _urlopen_response({"results": []})

        with mock.patch.object(Q.urllib.request, "urlopen", side_effect=fake_urlopen):
            Q.tavily_search("x", "tvly-test", base=self.base)
        usage = json.loads((self.base / "_tavily_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage["calls"], 1)


class SearchForGroupTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        asin_dir = self.base / "B0000000AA"
        asin_dir.mkdir(parents=True)
        (asin_dir / "amazon.json").write_text(
            json.dumps({"item": {"title": "レゴ クラシック 大きな創造ボックス"}}), encoding="utf-8",
        )

    def test_rejects_group_other_than_q1_q2(self):
        with self.assertRaises(ValueError):
            Q.search_for_group("Q0", "B0000000AA", "tvly-test", base=self.base)

    def test_q2_passes_include_domains_from_domains_module(self):
        from scripts.experimental.search_query_trial.domains import build_q2_include_domains

        captured = {}

        def fake_search(query, api_key, *, base, num=10, include_domains=None):
            captured["include_domains"] = include_domains
            return []

        with mock.patch.object(Q, "tavily_search", side_effect=fake_search):
            result = Q.search_for_group("Q2", "B0000000AA", "tvly-test", base=self.base)
        self.assertEqual(captured["include_domains"], build_q2_include_domains())
        self.assertEqual(result["group"], "Q2")

    def test_q1_has_no_include_domains(self):
        captured = {}

        def fake_search(query, api_key, *, base, num=10, include_domains=None):
            captured["include_domains"] = include_domains
            captured["query"] = query
            return []

        with mock.patch.object(Q, "tavily_search", side_effect=fake_search):
            Q.search_for_group("Q1", "B0000000AA", "tvly-test", base=self.base)
        self.assertIsNone(captured["include_domains"])
        self.assertIn("使ってみた", captured["query"])

    def test_missing_title_returns_empty_without_calling_tavily(self):
        with mock.patch.object(Q, "tavily_search") as fake:
            result = Q.search_for_group("Q1", "B0NOTFOUND1", "tvly-test", base=self.base)
        fake.assert_not_called()
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
