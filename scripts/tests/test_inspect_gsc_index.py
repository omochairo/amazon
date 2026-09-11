"""Unit tests for inspect_gsc_index (#3331)。

カバレッジ:
1. build_not_indexed_urls: verdict == "PASS" を除外し、それ以外を全件保持すること
2. 既定 (max_items=0) では 300 件を超えても切り捨てられないこと (#3331 の本題)
3. max_items > 0 を明示したときはその件数でキャップされること
4. None のフィールドが "(none)" に正規化されること
5. --max-not-indexed-urls の既定値が無制限 (0) であること
6. build_rich_fail_urls: PASS と (none) を除外し、課題つきで全件残すこと (#5085)
7. build_rich_fail_urls が last_crawl_time を落とさないこと —— 判定は「いまの
   ページ」ではなく「最後にクロールされた版」に対するもので、古い残像と現在の
   失敗を分ける材料がこれしかない (#5085)
8. select_target_urls / load_watchlist: watchlist 優先 + 残りローテーションが
   #7022 の意図どおり動くこと (watchlist は必ず含む、ローテーションが週ごとに
   ずれる、limit 以内なら全件返す、watchlist が limit を超える異常系も壊れない)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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


class BuildRichFailUrlsTest(unittest.TestCase):
    """#5085: リッチリザルトが落ちている URL を特定できること。"""

    @staticmethod
    def _item(url, rich_verdict="FAIL", rich_types=None, rich_issues=None):
        return {
            "url": url,
            "rich_verdict": rich_verdict,
            "rich_types": rich_types if rich_types is not None else ["商品スニペット"],
            "rich_issues": rich_issues if rich_issues is not None else ["ERROR: x"],
            "last_crawl_time": "2026-08-24T03:11:00Z",
        }

    def test_pass_and_none_are_excluded(self):
        inspected = [
            self._item("https://x/1", rich_verdict="PASS"),
            self._item("https://x/2", rich_verdict="(none)"),
            self._item("https://x/3", rich_verdict="FAIL"),
            self._item("https://x/4", rich_verdict="NEUTRAL"),
        ]
        rows = I.build_rich_fail_urls(inspected)
        self.assertEqual(["https://x/3", "https://x/4"], [r["url"] for r in rows])

    def test_missing_rich_verdict_is_treated_as_none(self):
        rows = I.build_rich_fail_urls([{"url": "https://x/1"}])
        self.assertEqual([], rows)

    def test_issues_are_kept_so_the_url_is_actionable(self):
        rows = I.build_rich_fail_urls([
            self._item("https://x/1", rich_issues=["ERROR: a", "WARNING: b"]),
        ])
        self.assertEqual(["ERROR: a", "WARNING: b"], rows[0]["rich_issues"])
        self.assertEqual(["商品スニペット"], rows[0]["rich_types"])

    def test_not_capped(self):
        inspected = [self._item("https://x/%d" % i) for i in range(470)]
        self.assertEqual(470, len(I.build_rich_fail_urls(inspected)))

    def test_last_crawl_time_is_kept(self):
        """判定対象は「最後にクロールされた版」。古い残像か現在の失敗かを
        分ける材料はこれしかないので落とさない (2026-08-30 の census で実際に
        詰まった: FAIL 18 件中 17 件が、生きているマークアップでは GSC の
        エラー文と食い違っていた)。"""
        rows = I.build_rich_fail_urls([self._item("https://x/1")])
        self.assertEqual("2026-08-24T03:11:00Z", rows[0]["last_crawl_time"])

    def test_missing_last_crawl_time_is_explicit_not_absent(self):
        """キーごと消すと『取れなかった』と『集計で落とした』が区別できない。"""
        rows = I.build_rich_fail_urls([
            {"url": "https://x/1", "rich_verdict": "FAIL"},
        ])
        self.assertEqual("(none)", rows[0]["last_crawl_time"])


class SelectTargetUrlsTest(unittest.TestCase):
    """#7022: watchlist 優先 + 残り予算のローテーションで全 URL がいずれ検査される。"""

    def test_returns_all_when_limit_covers_everything(self):
        urls = [f"https://x/{i}" for i in range(10)]
        got = I.select_target_urls(urls, watchlist=set(), limit=10, rotation_offset=0)
        self.assertEqual(urls, got)

    def test_returns_all_when_limit_is_zero_or_negative(self):
        urls = [f"https://x/{i}" for i in range(10)]
        self.assertEqual(urls, I.select_target_urls(urls, set(), limit=0, rotation_offset=3))
        self.assertEqual(urls, I.select_target_urls(urls, set(), limit=-1, rotation_offset=3))

    def test_watchlist_urls_are_always_included(self):
        urls = [f"https://x/{i}" for i in range(20)]
        watchlist = {"https://x/17", "https://x/3"}
        got = I.select_target_urls(urls, watchlist, limit=5, rotation_offset=0)
        self.assertTrue(watchlist.issubset(set(got)))
        self.assertEqual(5, len(got))

    def test_watchlist_urls_no_longer_in_sitemap_are_ignored(self):
        urls = [f"https://x/{i}" for i in range(5)]
        watchlist = {"https://gone/1"}
        got = I.select_target_urls(urls, watchlist, limit=3, rotation_offset=0)
        self.assertNotIn("https://gone/1", got)
        self.assertEqual(3, len(got))

    def test_rotation_shifts_the_window_across_offsets(self):
        """#7022 の本題: 同じ limit でもオフセットが進むと窓がずれ、数周期で
        watchlist 以外の全 URL がいずれ検査対象に入る。"""
        urls = [f"https://x/{i}" for i in range(100)]
        seen: set[str] = set()
        for offset in range(20):
            got = I.select_target_urls(urls, watchlist=set(), limit=10, rotation_offset=offset)
            self.assertEqual(10, len(got))
            seen.update(got)
        self.assertEqual(set(urls), seen)

    def test_rotation_window_wraps_around(self):
        urls = [f"https://x/{i}" for i in range(10)]
        got = I.select_target_urls(urls, watchlist=set(), limit=4, rotation_offset=2)
        # offset=2, budget=4 -> start = (2*4) % 10 = 8 -> [8, 9, 0, 1]
        self.assertEqual(["https://x/8", "https://x/9", "https://x/0", "https://x/1"], got)

    def test_watchlist_larger_than_limit_falls_back_to_alphabetical_head(self):
        urls = sorted(f"https://x/{i}" for i in range(10))
        watchlist = set(urls[:8])
        got = I.select_target_urls(urls, watchlist, limit=5, rotation_offset=7)
        self.assertEqual(5, len(got))
        self.assertTrue(set(got).issubset(watchlist))


class LoadWatchlistTest(unittest.TestCase):
    def test_missing_file_returns_empty_set(self):
        got = I.load_watchlist(Path("/nonexistent/census_url_states.json"))
        self.assertEqual(set(), got)

    def test_reads_states_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "census_url_states.json"
            p.write_text(json.dumps({
                "date": "2026-09-06",
                "states": {"https://x/1": "not_found_404", "https://x/2": "crawled_not_indexed"},
            }), encoding="utf-8")
            got = I.load_watchlist(p)
        self.assertEqual({"https://x/1", "https://x/2"}, got)

    def test_malformed_file_returns_empty_set_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "census_url_states.json"
            p.write_text("not json", encoding="utf-8")
            got = I.load_watchlist(p)
        self.assertEqual(set(), got)

    def test_unexpected_shape_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "census_url_states.json"
            p.write_text(json.dumps({"date": "2026-09-06"}), encoding="utf-8")
            got = I.load_watchlist(p)
        self.assertEqual(set(), got)


class EpochWeekIndexTest(unittest.TestCase):
    def test_monotonic_across_weeks(self):
        w1 = I.epoch_week_index(datetime(2026, 9, 6, tzinfo=timezone.utc))
        w2 = I.epoch_week_index(datetime(2026, 9, 13, tzinfo=timezone.utc))
        self.assertEqual(w1 + 1, w2)

    def test_does_not_wrap_at_year_boundary(self):
        """ISO week number は年境界で 52/53 -> 1 に戻るが、ローテーションの
        オフセットとしてはそれだと窓が巻き戻ってしまうため単調増加である必要がある。"""
        before = I.epoch_week_index(datetime(2026, 12, 28, tzinfo=timezone.utc))
        after = I.epoch_week_index(datetime(2027, 1, 4, tzinfo=timezone.utc))
        self.assertGreater(after, before)


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
