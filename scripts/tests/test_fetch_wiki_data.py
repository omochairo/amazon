"""fetch_wiki_data.py のユニットテスト (Issue #683 Phase 1)。

カバレッジ:
1. `_trim_extract` の整形 + 句点境界カット
2. `_is_fresh` TTL 判定
3. `fetch_summary` 200/404/disambig/timeout を mock で検証
4. `build_cache` の local 転記 + cache TTL 再利用 + limit 動作
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_wiki_data  # noqa: E402


def _mock_response(status: int = 200, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


class TrimExtractTest(unittest.TestCase):
    def test_short_text_returned_as_is(self):
        self.assertEqual(fetch_wiki_data._trim_extract("短い文。"), "短い文。")

    def test_collapses_whitespace(self):
        self.assertEqual(fetch_wiki_data._trim_extract("a  b\n\nc"), "a b c")

    def test_truncates_with_period_boundary(self):
        text = "あ" * 100 + "。" + "い" * 300
        out = fetch_wiki_data._trim_extract(text, limit=200)
        self.assertTrue(out.endswith("。"))
        self.assertLessEqual(len(out), 200)

    def test_truncates_with_ellipsis_when_no_boundary(self):
        out = fetch_wiki_data._trim_extract("あ" * 500, limit=100)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 101)


class IsFreshTest(unittest.TestCase):
    def test_recent_timestamp_is_fresh(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertTrue(fetch_wiki_data._is_fresh({"updated_at": ts}, timedelta(days=30)))

    def test_old_timestamp_not_fresh(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        self.assertFalse(fetch_wiki_data._is_fresh({"updated_at": ts}, timedelta(days=30)))

    def test_missing_or_bad_timestamp(self):
        self.assertFalse(fetch_wiki_data._is_fresh({}, timedelta(days=30)))
        self.assertFalse(fetch_wiki_data._is_fresh({"updated_at": "not-a-date"}, timedelta(days=30)))


class FetchSummaryTest(unittest.TestCase):
    def test_success(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {
            "title": "モンテッソーリ教育",
            "extract": "モンテッソーリ教育は、医師であり教育家であるマリア・モンテッソーリが考案した教育法である。",
            "content_urls": {"desktop": {"page": "https://ja.wikipedia.org/wiki/モンテッソーリ教育"}},
            "thumbnail": {"source": "https://upload.wikimedia.org/x.jpg"},
        })
        result = fetch_wiki_data.fetch_summary("モンテッソーリ教育", sess)
        self.assertEqual(result["type"], "wikipedia")
        self.assertEqual(result["title"], "モンテッソーリ教育")
        self.assertIn("モンテッソーリ", result["extract"])
        self.assertTrue(result["url"].startswith("https://"))
        self.assertEqual(result["thumbnail"], "https://upload.wikimedia.org/x.jpg")
        # User-Agent ヘッダが付いているか
        kw = sess.get.call_args.kwargs
        self.assertIn("User-Agent", kw["headers"])

    def test_404_returns_none(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(404)
        self.assertIsNone(fetch_wiki_data.fetch_summary("nonexistent", sess))

    def test_disambiguation_returns_none(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"type": "disambiguation", "extract": "x"})
        self.assertIsNone(fetch_wiki_data.fetch_summary("Foo", sess))

    def test_empty_extract_returns_none(self):
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, {"title": "x", "extract": ""})
        self.assertIsNone(fetch_wiki_data.fetch_summary("x", sess))

    def test_network_error_returns_none(self):
        import requests as _requests
        sess = MagicMock()
        sess.get.side_effect = _requests.Timeout()
        self.assertIsNone(fetch_wiki_data.fetch_summary("x", sess))


class BuildCacheTest(unittest.TestCase):
    def test_local_entry_copied_directly(self):
        mapping = {"グッド・トイ": {"type": "local", "title": "グッド・トイ", "extract": "...", "url": "https://goodtoy.jp/"}}
        with patch.object(fetch_wiki_data, "fetch_summary") as m:
            entries, stats = fetch_wiki_data.build_cache(mapping, {}, ttl_days=30, sleep_sec=0)
            m.assert_not_called()
        self.assertEqual(stats["local"], 1)
        self.assertEqual(entries["グッド・トイ"]["type"], "local")
        self.assertEqual(entries["グッド・トイ"]["url"], "https://goodtoy.jp/")

    def test_fresh_cache_skips_api(self):
        mapping = {"K": {"type": "wikipedia", "target": "K"}}
        prior = {"K": {"type": "wikipedia", "title": "K", "extract": "x", "url": "u",
                      "updated_at": datetime.now(timezone.utc).isoformat()}}
        with patch.object(fetch_wiki_data, "fetch_summary") as m:
            entries, stats = fetch_wiki_data.build_cache(mapping, prior, ttl_days=30, sleep_sec=0)
            m.assert_not_called()
        self.assertEqual(stats["wiki_fresh"], 1)
        self.assertEqual(entries["K"], prior["K"])

    def test_stale_cache_refetches(self):
        mapping = {"K": {"type": "wikipedia", "target": "K"}}
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        prior = {"K": {"type": "wikipedia", "title": "K", "extract": "x", "url": "u", "updated_at": old}}
        fake = {"type": "wikipedia", "title": "K", "extract": "new", "url": "u",
                "thumbnail": None, "updated_at": datetime.now(timezone.utc).isoformat()}
        with patch.object(fetch_wiki_data, "fetch_summary", return_value=fake):
            entries, stats = fetch_wiki_data.build_cache(mapping, prior, ttl_days=30, sleep_sec=0)
        self.assertEqual(stats["wiki_fetched"], 1)
        self.assertEqual(entries["K"]["extract"], "new")

    def test_limit_caps_api_calls_but_reuses_cached(self):
        mapping = {f"K{i}": {"type": "wikipedia", "target": f"K{i}"} for i in range(5)}
        prior = {"K0": {"type": "wikipedia", "title": "K0", "extract": "old", "url": "u",
                        "updated_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()}}
        fake = {"type": "wikipedia", "title": "x", "extract": "x", "url": "u",
                "thumbnail": None, "updated_at": datetime.now(timezone.utc).isoformat()}
        with patch.object(fetch_wiki_data, "fetch_summary", return_value=fake) as m:
            entries, stats = fetch_wiki_data.build_cache(mapping, prior, ttl_days=30, sleep_sec=0, limit=2)
            self.assertEqual(m.call_count, 2)
        self.assertEqual(stats["wiki_fetched"], 2)
        # K0 はキャッシュがあるので reused (fresh ではないが limit 到達後に流用)
        self.assertIn("K0", entries)


if __name__ == "__main__":
    unittest.main()
