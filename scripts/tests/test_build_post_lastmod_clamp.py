"""Unit tests for #2814 — clamp lastmod to publish date when it precedes it.

Jules sets date to JST 10:00 of the run day; PR auto-merge runs in UTC, so the
git commit time (lastmod) can roll back hours/a-day, producing dateModified <
datePublished in JSON-LD, reversed sitemap <lastmod>, and a bogus
"最終更新: 前日" in post_meta. _clamp_lastmod_to_date fixes this.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _clamp_lastmod_to_date  # type: ignore[import-not-found]


class ClampLastmodTests(unittest.TestCase):
    def test_utc_commit_before_jst_publish_is_clamped(self):
        # issue の実例: lastmod (UTC) は date (JST) より実時点で過去
        date = "2026-06-23T10:00:00+09:00"
        lastmod = "2026-06-22T17:11:10+00:00"  # = 2026-06-23T02:11:10+09:00
        self.assertEqual(_clamp_lastmod_to_date(lastmod, date), date)

    def test_z_suffix_is_parsed(self):
        date = "2026-06-23T10:00:00+09:00"
        lastmod = "2026-06-22T17:11:10Z"
        self.assertEqual(_clamp_lastmod_to_date(lastmod, date), date)

    def test_genuine_later_update_is_preserved(self):
        # 後日の正当な更新は lastmod を維持 (これが本来の「最終更新」表示)
        date = "2026-06-23T10:00:00+09:00"
        lastmod = "2026-06-28T05:00:00+00:00"
        self.assertEqual(_clamp_lastmod_to_date(lastmod, date), lastmod)

    def test_same_moment_is_preserved(self):
        date = "2026-06-23T10:00:00+09:00"
        self.assertEqual(_clamp_lastmod_to_date(date, date), date)

    def test_naive_aware_mismatch_falls_back_to_date_string(self):
        # naive lastmod vs aware date → 日付文字列比較で前日なら同期
        date = "2026-06-23T10:00:00+09:00"
        lastmod = "2026-06-22T17:11:10"  # naive
        self.assertEqual(_clamp_lastmod_to_date(lastmod, date), date)

    def test_naive_aware_mismatch_same_day_preserved(self):
        date = "2026-06-23T10:00:00+09:00"
        lastmod = "2026-06-23T01:00:00"  # naive, 同日
        self.assertEqual(_clamp_lastmod_to_date(lastmod, date), lastmod)

    def test_empty_values_passthrough(self):
        self.assertEqual(_clamp_lastmod_to_date("", "2026-06-23"), "")
        self.assertEqual(_clamp_lastmod_to_date("2026-06-23", ""), "2026-06-23")


if __name__ == "__main__":
    unittest.main()
