"""Tests for backfill_rewrite_ledger.py (amazon-navi-brain#39 Step 0-a).

parse_deletions() is a pure function over synthetic `git log` output so these
tests never touch the real repo history. build_ledger_records() is tested
against a temp articles_dir to control which ASINs currently have a live body.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from scripts import backfill_rewrite_ledger as brl

ASIN = "B00I7JXEEA"
ASIN2 = "B01234ABCD"


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{}")


SAMPLE_LOG = f"""@@abc123|2026-07-01T00:00:00+00:00
data/articles/2026-05-01-{ASIN}.json

@@def456|2026-08-01T00:00:00+00:00
data/articles/2026-05-01-{ASIN2}.json
data/articles/2026-05-01-{ASIN}.quality.json
"""


class ParseDeletionsTest(unittest.TestCase):
    def test_parses_slug_asin_and_date(self) -> None:
        out = brl.parse_deletions(SAMPLE_LOG)
        self.assertEqual(len(out), 2, "sidecar (.quality.json) must be excluded")
        self.assertEqual(out[0], {
            "old_slug": f"2026-05-01-{ASIN}",
            "asin": ASIN,
            "commit_date": "2026-07-01T00:00:00+00:00",
        })
        self.assertEqual(out[1]["asin"], ASIN2)
        self.assertEqual(out[1]["commit_date"], "2026-08-01T00:00:00+00:00")

    def test_empty_input_yields_no_rows(self) -> None:
        self.assertEqual(brl.parse_deletions(""), [])


class LatestDeletionPerAsinTest(unittest.TestCase):
    def test_keeps_most_recent_commit_date(self) -> None:
        deletions = [
            {"old_slug": f"2026-05-01-{ASIN}", "asin": ASIN, "commit_date": "2026-06-01T00:00:00+00:00"},
            {"old_slug": f"2026-06-15-{ASIN}", "asin": ASIN, "commit_date": "2026-07-01T00:00:00+00:00"},
        ]
        latest = brl.latest_deletion_per_asin(deletions)
        self.assertEqual(latest[ASIN]["old_slug"], f"2026-06-15-{ASIN}")


class BuildLedgerRecordsTest(unittest.TestCase):
    def test_emits_row_when_newer_body_survives(self) -> None:
        with tempfile.TemporaryDirectory() as articles_dir:
            new_slug = f"2026-08-01-{ASIN}"
            _touch(os.path.join(articles_dir, f"{new_slug}.json"))
            deletions = [{"old_slug": f"2026-05-01-{ASIN}", "asin": ASIN,
                          "commit_date": "2026-07-01T00:00:00+00:00"}]
            records = brl.build_ledger_records(deletions, articles_dir)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0], {
                "asin": ASIN, "old_slug": f"2026-05-01-{ASIN}",
                "new_slug": new_slug, "completed_at": "2026-07-01T00:00:00+00:00",
                "source": "backfill",
            })

    def test_skips_asin_with_no_surviving_body(self) -> None:
        with tempfile.TemporaryDirectory() as articles_dir:
            deletions = [{"old_slug": f"2026-05-01-{ASIN}", "asin": ASIN,
                          "commit_date": "2026-07-01T00:00:00+00:00"}]
            records = brl.build_ledger_records(deletions, articles_dir)
            self.assertEqual(records, [])

    def test_skips_when_survivor_is_the_deleted_slug_itself(self) -> None:
        """A delete-then-re-add-with-identical-slug is not a completed rewrite."""
        with tempfile.TemporaryDirectory() as articles_dir:
            old_slug = f"2026-05-01-{ASIN}"
            _touch(os.path.join(articles_dir, f"{old_slug}.json"))
            deletions = [{"old_slug": old_slug, "asin": ASIN,
                          "commit_date": "2026-07-01T00:00:00+00:00"}]
            records = brl.build_ledger_records(deletions, articles_dir)
            self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
