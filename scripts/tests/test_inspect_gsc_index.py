"""Unit tests for inspect_gsc_index (#3331)。

カバレッジ:
1. build_not_indexed_urls: verdict == "PASS" を除外し、それ以外を全件保持すること
2. 既定 (max_items=0) では 300 件を超えても切り捨てられないこと (#3331 の本題)
3. max_items > 0 を明示したときはその件数でキャップされること
4. None のフィールドが "(none)" に正規化されること
5. --max-not-indexed-urls の既定値が無制限 (0) であること
"""
from __future__ import annotations

import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import inspect_gsc_index as I  # noqa: E402


def _item(url: str, verdict: str = "NEUTRAL", **kw) -> dict:
    row = {
        "url": url,
        "verdict": verdict,
        "coverage_state": "見つかりませんでした（404）",
        "last_crawl_time": "2026-06-10T15:17:52Z",
        "google_canonical": None,
    }
    row.update(kw)
    return row


class TestBuildNotIndexedUrls(unittest.TestCase):
    def test_excludes_pass_and_keeps_the_rest(self):
        inspected = [
            _item("https://x/1", verdict="PASS"),
            _item("https://x/2", verdict="NEUTRAL"),
            _item("https://x/3", verdict="FAIL"),
        ]
        rows = I.build_not_indexed_urls(inspected)
        self.assertEqual(["https://x/2", "https://x/3"], [r["url"] for r in rows])

    def test_default_keeps_all_beyond_the_old_300_cap(self):
        inspected = [_item("https://x/%d" % i) for i in range(470)]
        rows = I.build_not_indexed_urls(inspected)
        self.assertEqual(470, len(rows))

    def test_explicit_max_items_caps(self):
        inspected = [_item("https://x/%d" % i) for i in range(470)]
        rows = I.build_not_indexed_urls(inspected, max_items=50)
        self.assertEqual(50, len(rows))
        self.assertEqual("https://x/0", rows[0]["url"])

    def test_none_fields_are_normalized(self):
        rows = I.build_not_indexed_urls([
            _item("https://x/1", coverage_state=None, last_crawl_time=None, google_canonical=None),
        ])
        self.assertEqual("(none)", rows[0]["coverage_state"])
        self.assertEqual("(none)", rows[0]["last_crawl_time"])
        self.assertEqual("(none)", rows[0]["google_canonical"])

    def test_default_cap_constant_is_unlimited(self):
        self.assertEqual(0, I.DEFAULT_MAX_NOT_INDEXED_URLS)


if __name__ == "__main__":
    unittest.main()
