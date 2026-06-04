"""Unit tests for fetch_third_party_sources (#1600 Phase 2).

ネットワークを使わない純ロジック (host 抽出 / 除外判定 / source 整形 / freshness) を検証。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import pathlib
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_third_party_sources as F  # noqa: E402


class HostFilterTest(unittest.TestCase):
    def test_host_strips_www(self):
        self.assertEqual(F._host("https://www.example.com/path"), "example.com")
        self.assertEqual(F._host("https://blog.example.jp/x"), "blog.example.jp")

    def test_retail_excluded(self):
        for u in (
            "https://www.amazon.co.jp/dp/B0XXXX",
            "https://item.rakuten.co.jp/shop/abc/",
            "https://store.shopping.yahoo.co.jp/x",
            "https://jp.mercari.com/item/m123",
        ):
            self.assertTrue(F._is_excluded(u), u)

    def test_search_engine_and_own_site_excluded(self):
        self.assertTrue(F._is_excluded("https://www.google.com/search?q=x"))
        self.assertTrue(F._is_excluded("https://navi.omcha.jp/posts/foo/"))

    def test_editorial_kept(self):
        for u in (
            "https://review.kakaku.com/review/K0001/",
            "https://mybest.example/best-toys",
            "https://www.itmedia.co.jp/news/article.html",
        ):
            self.assertFalse(F._is_excluded(u), u)

    def test_non_http_excluded(self):
        self.assertTrue(F._is_excluded("ftp://example.com/x"))
        self.assertTrue(F._is_excluded(""))


class FilterSourcesTest(unittest.TestCase):
    def test_dedupe_by_host_and_cap(self):
        raw = [
            {"link": "https://a.example.com/1", "title": "A1", "snippet": "s"},
            {"link": "https://a.example.com/2", "title": "A2", "snippet": "s"},  # 同 host
            {"link": "https://www.amazon.co.jp/dp/X", "title": "buy"},           # retail 除外
            {"link": "https://b.example.com/x", "title": "B", "snippet": "t"},
            {"link": "https://c.example.com/y", "title": "C"},
        ]
        out = F._filter_sources(raw, max_sources=2)
        self.assertEqual(len(out), 2)
        self.assertEqual([s["host"] for s in out], ["a.example.com", "b.example.com"])
        self.assertEqual(out[0]["title"], "A1")


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = pathlib.Path(self._tmp.name) / "tp.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, ts: str):
        self.p.write_text(json.dumps({"fetched_at": ts}), encoding="utf-8")

    def test_recent_is_fresh(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write(now)
        self.assertTrue(F._is_fresh(self.p, 30))

    def test_old_is_stale(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
        self._write(old)
        self.assertFalse(F._is_fresh(self.p, 30))

    def test_missing_is_stale(self):
        self.assertFalse(F._is_fresh(self.p, 30))


if __name__ == "__main__":
    unittest.main()
