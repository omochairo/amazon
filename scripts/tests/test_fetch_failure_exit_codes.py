"""fetch_yahoo_news / fetch_books / fetch_engagement_trends / fetch_wiki_data /
internal_links(strict) の「取得失敗を 0 件と区別して非ゼロ終了にする」挙動のテスト。

方針 (PR #12107 と同じ): 全件失敗なら 1・何も書かない・queried 印を付けない。
一部失敗は警告にとどめ、失敗した分だけ「済」にしない。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_books  # noqa: E402
import fetch_engagement_trends  # noqa: E402
import fetch_wiki_data  # noqa: E402
import fetch_yahoo_news  # noqa: E402
import internal_links  # noqa: E402
import requests  # noqa: E402


def _resp(status=200, content=b"", json_body=None):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.json.return_value = json_body if json_body is not None else {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    return r


RSS_EMPTY = b'<?xml version="1.0"?><rss><channel></channel></rss>'


class _TmpMixin(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = pathlib.Path(self._td.name)
        self.out = self.root / "raw"
        self.out.mkdir()
        self.articles = self.root / "articles"
        self.articles.mkdir()
        (self.out / "amazon.json").write_text(
            json.dumps({"items": [
                {"asin": "B000AAAA01", "title": "レゴ クラシック 黄色のアイデアボックス"},
                {"asin": "B000BBBB02", "title": "トミカ はたらくくるま ひろばセット"},
            ]}), encoding="utf-8")


class YahooNewsTest(_TmpMixin):
    def _run(self, side_effect):
        argv = ["fetch_yahoo_news.py", "--out", str(self.out),
                "--articles-dir", str(self.articles)]
        with patch.object(sys, "argv", argv), \
             patch.object(fetch_yahoo_news.requests, "get", side_effect=side_effect):
            return fetch_yahoo_news.main()

    def test_gnews_search_reports_error_but_not_zero_hits(self):
        errs: list = []
        with patch.object(fetch_yahoo_news.requests, "get", return_value=_resp(503)):
            self.assertEqual(fetch_yahoo_news.gnews_search("q", errors=errs), [])
        self.assertTrue(errs)
        errs = []
        with patch.object(fetch_yahoo_news.requests, "get",
                          return_value=_resp(200, RSS_EMPTY)):
            self.assertEqual(fetch_yahoo_news.gnews_search("q", errors=errs), [])
        self.assertEqual(errs, [])

    def test_all_fail_returns_1_and_writes_nothing(self):
        rc = self._run(lambda *a, **k: _resp(503))
        self.assertEqual(rc, 1)
        self.assertFalse((self.out / "news.json").exists())
        self.assertFalse((self.out / "per_asin").exists())
        self.assertFalse((self.out / "_fetch_state.json").exists())

    def test_all_zero_hits_is_success(self):
        rc = self._run(lambda *a, **k: _resp(200, RSS_EMPTY))
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / "news.json").exists())

    def test_partial_failure_keeps_going_and_skips_failed_asin(self):
        calls = {"n": 0}

        def side(*a, **k):
            calls["n"] += 1
            # 1 回目 (ジャンル) と 2 回目 (1 本目の ASIN) は成功、以降は失敗
            return _resp(200, RSS_EMPTY) if calls["n"] <= 2 else _resp(500)

        rc = self._run(side)
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / "news.json").exists())
        done = [p.name for p in (self.out / "per_asin").glob("*")] \
            if (self.out / "per_asin").exists() else []
        self.assertEqual(len(done), 1)  # 失敗した ASIN の空記録は書かれない


class BooksTest(_TmpMixin):
    def _run(self, side_effect, key="k"):
        argv = ["fetch_books.py", "知育", "--out", str(self.out),
                "--articles-dir", str(self.articles)]
        env = {"GOOGLEBOOKS_API_KEY": key} if key else {}
        with patch.object(sys, "argv", argv), \
             patch.dict(os.environ, env, clear=False), \
             patch.object(fetch_books.requests, "get", side_effect=side_effect):
            if not key:
                os.environ.pop("GOOGLEBOOKS_API_KEY", None)
            return fetch_books.main()

    def test_books_search_reports_error_but_not_zero_hits(self):
        errs: list = []
        with patch.object(fetch_books.requests, "get", return_value=_resp(503)):
            self.assertEqual(fetch_books.books_search("k", "q", errors=errs), [])
        self.assertTrue(errs)
        errs = []
        with patch.object(fetch_books.requests, "get", return_value=_resp(403)):
            self.assertEqual(fetch_books.books_search("k", "q", errors=errs), [])
        self.assertTrue(errs)
        errs = []
        with patch.object(fetch_books.requests, "get",
                          return_value=_resp(200, json_body={"items": []})):
            self.assertEqual(fetch_books.books_search("k", "q", errors=errs), [])
        self.assertEqual(errs, [])

    def test_missing_key_returns_1_and_does_not_overwrite(self):
        target = self.out / "books_result.json"
        target.write_text('{"keyword": "x", "items": [{"title": "keep"}]}', encoding="utf-8")
        rc = self._run(lambda *a, **k: _resp(200, json_body={}), key="")
        self.assertEqual(rc, 1)
        self.assertIn("keep", target.read_text(encoding="utf-8"))

    def test_all_fail_returns_1_and_writes_nothing(self):
        rc = self._run(lambda *a, **k: _resp(403))
        self.assertEqual(rc, 1)
        self.assertFalse((self.out / "books_result.json").exists())
        self.assertFalse((self.out / "per_asin").exists())
        self.assertFalse((self.out / "_fetch_state.json").exists())

    def test_all_zero_hits_is_success(self):
        rc = self._run(lambda *a, **k: _resp(200, json_body={"items": []}))
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / "books_result.json").exists())

    def test_partial_failure_returns_0(self):
        calls = {"n": 0}

        def side(*a, **k):
            calls["n"] += 1
            return _resp(200, json_body={"items": []}) if calls["n"] == 1 else _resp(500)

        rc = self._run(side)
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / "books_result.json").exists())


class EngagementTrendsTest(_TmpMixin):
    def _run(self, side_effect):
        argv = ["fetch_engagement_trends.py", "--out-dir", str(self.root / "trends"),
                "--date", "2026-01-01"]
        with patch.object(sys, "argv", argv), \
             patch.object(fetch_engagement_trends.requests, "get", side_effect=side_effect), \
             patch.object(fetch_engagement_trends.time, "sleep"):
            return fetch_engagement_trends.main()

    def test_all_feeds_fail_returns_1_and_writes_nothing(self):
        rc = self._run(requests.ConnectionError("down"))
        self.assertEqual(rc, 1)
        self.assertFalse((self.root / "trends" / "2026-01-01.json").exists())

    def test_html_block_page_counts_as_failure(self):
        rc = self._run(lambda *a, **k: _resp(200, b"<html><body>blocked</body></html>"))
        self.assertEqual(rc, 1)

    def test_empty_but_healthy_feeds_is_success(self):
        rc = self._run(lambda *a, **k: _resp(200, RSS_EMPTY))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "trends" / "2026-01-01.json").exists())

    def test_partial_feed_failure_returns_0(self):
        calls = {"n": 0}

        def side(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp(200, RSS_EMPTY)
            raise requests.ConnectionError("down")

        rc = self._run(side)
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "trends" / "2026-01-01.json").exists())


class WikiDataTest(_TmpMixin):
    def _mapping(self):
        return {"A": {"type": "wikipedia", "target": "A"},
                "B": {"type": "wikipedia", "target": "B"}}

    def test_fetch_summary_error_vs_404(self):
        s = requests.Session()
        errs: list = []
        with patch.object(s, "get", return_value=_resp(503)):
            self.assertIsNone(fetch_wiki_data.fetch_summary("A", s, errors=errs))
        self.assertTrue(errs)
        errs = []
        with patch.object(s, "get", return_value=_resp(404)):
            self.assertIsNone(fetch_wiki_data.fetch_summary("A", s, errors=errs))
        self.assertEqual(errs, [])

    def test_failure_keeps_stale_cached_entry(self):
        stale = {"type": "wikipedia", "title": "A", "extract": "old", "url": "u",
                 "thumbnail": None, "updated_at": "2000-01-01T00:00:00+00:00"}

        def fake(target, session, timeout=8.0, source_key=None, errors=None):
            if errors is not None:
                errors.append("HTTP 503")
            return None

        with patch.object(fetch_wiki_data, "fetch_summary", side_effect=fake), \
             patch.object(fetch_wiki_data.time, "sleep"):
            entries, stats = fetch_wiki_data.build_cache(
                self._mapping(), {"A": stale}, ttl_days=30, sleep_sec=0)
        self.assertEqual(entries["A"]["extract"], "old")
        self.assertNotIn("B", entries)
        self.assertEqual(stats["wiki_failed"], 2)
        self.assertEqual(stats["wiki_attempted"], 2)
        self.assertEqual(stats["wiki_skipped"], 0)

    def _run_main(self, fake):
        mp = self.root / "map.json"
        mp.write_text(json.dumps(self._mapping()), encoding="utf-8")
        out = self.root / "cache.json"
        argv = ["fetch_wiki_data.py", "--mapping", str(mp), "--out", str(out), "--sleep", "0"]
        with patch.object(sys, "argv", argv), \
             patch.object(fetch_wiki_data, "fetch_summary", side_effect=fake), \
             patch.object(fetch_wiki_data.time, "sleep"):
            return fetch_wiki_data.main(), out

    def test_all_failed_returns_1_and_writes_nothing(self):
        def fake(target, session, timeout=8.0, source_key=None, errors=None):
            errors.append("net")
            return None

        rc, out = self._run_main(fake)
        self.assertEqual(rc, 1)
        self.assertFalse(out.exists())

    def test_all_404_is_success(self):
        rc, out = self._run_main(lambda *a, **k: None)
        self.assertEqual(rc, 0)
        self.assertTrue(out.exists())

    def test_partial_failure_returns_0(self):
        def fake(target, session, timeout=8.0, source_key=None, errors=None):
            if target == "A":
                errors.append("net")
                return None
            return {"type": "wikipedia", "title": "B", "extract": "x", "url": "u",
                    "thumbnail": None,
                    "updated_at": datetime.now(timezone.utc).isoformat()}

        rc, out = self._run_main(fake)
        self.assertEqual(rc, 0)
        self.assertIn("B", json.loads(out.read_text(encoding="utf-8"))["entries"])


class InternalLinksStrictTest(unittest.TestCase):
    def test_strict_raises_on_request_failure(self):
        with patch.object(internal_links.requests, "get",
                          side_effect=requests.ConnectionError("down")):
            self.assertEqual(internal_links.get_related_articles("kw"), [])
            with self.assertRaises(requests.ConnectionError):
                internal_links.get_related_articles("kw", strict=True)

    def test_strict_raises_on_malformed_body(self):
        r = _resp(200, json_body={"unexpected": 1})
        with patch.object(internal_links.requests, "get", return_value=r):
            self.assertEqual(internal_links.get_related_articles("kw"), [])
            with self.assertRaises(ValueError):
                internal_links.get_related_articles("kw", strict=True)

    def test_strict_empty_results_is_not_error(self):
        r = _resp(200, json_body={"results": []})
        with patch.object(internal_links.requests, "get", return_value=r):
            self.assertEqual(internal_links.get_related_articles("kw", strict=True), [])


if __name__ == "__main__":
    unittest.main()
