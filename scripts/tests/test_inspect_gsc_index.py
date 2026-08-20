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


class SummarizeRichResultsTest(unittest.TestCase):
    """#5085: richResultsResult を集計形に潰す。"""

    def test_missing_rich_results_is_none_not_empty_pass(self):
        """リッチリザルトが無効なとき GSC は richResultsResult ごと返さない。
        これを PASS や「課題リスト空」と混同すると「有効」と誤読する。"""
        for empty in (None, {}, "not a dict"):
            got = I._summarize_rich_results(empty)
            self.assertEqual(got["verdict"], "(none)")
            self.assertEqual(got["types"], [])
            self.assertEqual(got["issues"], [])

    def test_detected_types_are_collected(self):
        got = I._summarize_rich_results({
            "verdict": "PASS",
            "detectedItems": [
                {"richResultType": "Product snippets", "items": [{"name": "x"}]},
                {"richResultType": "Merchant listings", "items": [{"name": "y"}]},
            ],
        })
        self.assertEqual(got["verdict"], "PASS")
        self.assertEqual(got["types"], ["Merchant listings", "Product snippets"])
        self.assertEqual(got["issues"], [])

    def test_issues_carry_type_and_severity(self):
        got = I._summarize_rich_results({
            "verdict": "PARTIAL",
            "detectedItems": [{
                "richResultType": "Product snippets",
                "items": [{"name": "x", "issues": [
                    {"issueMessage": "Invalid object type for field 'review'",
                     "severity": "ERROR"},
                    {"issueMessage": "Missing field 'aggregateRating'",
                     "severity": "WARNING"},
                ]}],
            }],
        })
        self.assertEqual(got["verdict"], "PARTIAL")
        self.assertIn("Product snippets / ERROR: Invalid object type for field 'review'",
                      got["issues"])
        self.assertIn("Product snippets / WARNING: Missing field 'aggregateRating'",
                      got["issues"])

    def test_duplicate_issues_are_deduped_per_url(self):
        """同じ課題が複数 item に出ても URL 1 本ぶんとして数える。"""
        got = I._summarize_rich_results({
            "verdict": "PARTIAL",
            "detectedItems": [{
                "richResultType": "Product snippets",
                "items": [
                    {"issues": [{"issueMessage": "same", "severity": "WARNING"}]},
                    {"issues": [{"issueMessage": "same", "severity": "WARNING"}]},
                ],
            }],
        })
        self.assertEqual(len(got["issues"]), 1)

    def test_malformed_entries_do_not_raise(self):
        got = I._summarize_rich_results({
            "verdict": "FAIL",
            "detectedItems": [None, {"items": "nope"}, {"richResultType": "T",
                                                        "items": [None]}],
        })
        self.assertEqual(got["verdict"], "FAIL")
        self.assertEqual(got["types"], ["(unnamed)", "T"])
