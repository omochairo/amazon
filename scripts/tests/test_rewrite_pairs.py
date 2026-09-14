"""scripts/experimental/multistage_brief/rewrite_pairs.py unit tests (#4841 M1-c)。"""
from __future__ import annotations

import unittest

from scripts.experimental.multistage_brief.rewrite_pairs import (
    evaluate_pair,
    find_rewrite_candidates,
    parse_add_events,
)


class ParseAddEventsTest(unittest.TestCase):
    def test_groups_by_asin_and_keeps_metadata(self):
        log_text = (
            "C sha1 2026-05-22T10:00:00+09:00\n"
            "data/articles/2026-05-22-B0000000AA.json\n"
            "C sha2 2026-09-14T10:00:00+09:00\n"
            "data/articles/2026-09-14-B0000000AA.json\n"
            "data/articles/2026-09-14-B0000000BB.json\n"
        )
        events = parse_add_events(log_text)
        self.assertEqual(len(events["B0000000AA"]), 2)
        self.assertEqual(events["B0000000AA"][0]["sha"], "sha1")
        self.assertEqual(events["B0000000AA"][0]["date_prefix"], "2026-05-22")
        self.assertEqual(events["B0000000AA"][1]["sha"], "sha2")
        self.assertEqual(len(events["B0000000BB"]), 1)

    def test_ignores_non_matching_lines(self):
        log_text = "C sha1 2026-05-22T10:00:00+09:00\nsome/other/path.json\n"
        self.assertEqual(parse_add_events(log_text), {})

    def test_empty_log(self):
        self.assertEqual(parse_add_events(""), {})


class FindRewriteCandidatesTest(unittest.TestCase):
    def test_finds_asin_with_two_distinct_dates(self):
        events = {
            "B0000000AA": [
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "old_sha", "commit_at": "2026-05-22T10:00:00+09:00"},
                {"date_prefix": "2026-09-14", "filename": "data/articles/2026-09-14-B0000000AA.json",
                 "sha": "new_sha", "commit_at": "2026-09-14T10:00:00+09:00"},
            ],
        }
        result = find_rewrite_candidates(events, {"B0000000AA"})
        self.assertEqual(result["B0000000AA"]["earliest"]["sha"], "old_sha")
        self.assertEqual(result["B0000000AA"]["latest_filename"], "data/articles/2026-09-14-B0000000AA.json")
        self.assertEqual(result["B0000000AA"]["distinct_version_count"], 2)

    def test_single_version_is_not_a_candidate(self):
        events = {
            "B0000000AA": [
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "sha1", "commit_at": "2026-05-22T10:00:00+09:00"},
            ],
        }
        self.assertEqual(find_rewrite_candidates(events, {"B0000000AA"}), {})

    def test_repeated_additions_of_same_date_do_not_count_as_two_versions(self):
        events = {
            "B0000000AA": [
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "sha1", "commit_at": "2026-05-22T10:00:00+09:00"},
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "sha1b", "commit_at": "2026-06-22T10:00:00+09:00"},
            ],
        }
        self.assertEqual(find_rewrite_candidates(events, {"B0000000AA"}), {})

    def test_earliest_picks_oldest_commit_among_same_date(self):
        events = {
            "B0000000AA": [
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "restored", "commit_at": "2026-06-22T10:00:00+09:00"},
                {"date_prefix": "2026-05-22", "filename": "data/articles/2026-05-22-B0000000AA.json",
                 "sha": "original", "commit_at": "2026-05-22T10:00:00+09:00"},
                {"date_prefix": "2026-09-14", "filename": "data/articles/2026-09-14-B0000000AA.json",
                 "sha": "new_sha", "commit_at": "2026-09-14T10:00:00+09:00"},
            ],
        }
        result = find_rewrite_candidates(events, {"B0000000AA"})
        self.assertEqual(result["B0000000AA"]["earliest"]["sha"], "original")

    def test_asin_not_in_target_set_excluded(self):
        events = {
            "B0000000AA": [
                {"date_prefix": "2026-05-22", "filename": "x", "sha": "s1", "commit_at": "2026-05-22T10:00:00+09:00"},
                {"date_prefix": "2026-09-14", "filename": "y", "sha": "s2", "commit_at": "2026-09-14T10:00:00+09:00"},
            ],
        }
        self.assertEqual(find_rewrite_candidates(events, {"B0000000ZZ"}), {})


class EvaluatePairTest(unittest.TestCase):
    def test_valid_pair_old_before_new_after_generated_at(self):
        old = {"date": "2026-05-22T00:00:00Z", "narrative": {"lead": "旧"}}
        new = {"date": "2026-09-14T00:00:00Z", "narrative": {"lead": "新"}}
        result = evaluate_pair("B0000000AA", old, new, "2026-08-01T00:00:00Z")
        self.assertIsNotNone(result)
        self.assertEqual(result["old_narrative"], {"lead": "旧"})
        self.assertEqual(result["new_narrative"], {"lead": "新"})

    def test_invalid_when_old_is_also_after_material(self):
        old = {"date": "2026-09-01T00:00:00Z", "narrative": {}}
        new = {"date": "2026-09-14T00:00:00Z", "narrative": {}}
        self.assertIsNone(evaluate_pair("B0000000AA", old, new, "2026-08-01T00:00:00Z"))

    def test_invalid_when_new_is_also_before_material(self):
        old = {"date": "2026-05-01T00:00:00Z", "narrative": {}}
        new = {"date": "2026-05-22T00:00:00Z", "narrative": {}}
        self.assertIsNone(evaluate_pair("B0000000AA", old, new, "2026-08-01T00:00:00Z"))

    def test_missing_article_or_date_returns_none(self):
        self.assertIsNone(evaluate_pair("A", None, {"date": "2026-09-14T00:00:00Z"}, "2026-08-01T00:00:00Z"))
        self.assertIsNone(evaluate_pair("A", {"date": "not-a-date"}, {"date": "2026-09-14T00:00:00Z"}, "2026-08-01T00:00:00Z"))
        self.assertIsNone(evaluate_pair("A", {"date": "2026-05-01T00:00:00Z"}, {"date": "2026-09-14T00:00:00Z"}, None))


if __name__ == "__main__":
    unittest.main()
