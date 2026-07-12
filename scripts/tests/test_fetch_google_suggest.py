"""Unit tests for fetch_google_suggest (#2953 D案)。

カバレッジ:
1. 対象選定 (sidecar 除外・rewrite 新旧併存の winner 選択・ASIN 抽出)
2. select_targets (未取得優先・fetched_at 昇順・min-age-days・limit cap)
3. サジェストパース (_extract_suggestions) と正規化重複除去 (dedupe_suggestions)、
   seed 自身の除外
4. run(): 非200 / 接続エラーで graceful abort + 部分保存、JSON パース失敗の
   連続3回で打ち切り、dry-run が書き込みしないこと
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_google_suggest as F  # noqa: E402


def _write_article(dir_path: pathlib.Path, filename: str, product_name: str = "テスト商品") -> pathlib.Path:
    p = dir_path / filename
    p.write_text(
        json.dumps({"product": {"name": product_name}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def _resp(status: int = 200, json_list=None, text: str | None = None):
    r = mock.Mock()
    r.status_code = status
    if text is not None:
        r.text = text
    else:
        r.text = json.dumps(json_list if json_list is not None else ["seed", []])
    return r


class DiscoverTargetAsinsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_excludes_sidecars(self):
        _write_article(self.articles_dir, "2026-05-01-B0XXXXXXXX.json")
        (self.articles_dir / "2026-05-01-B0XXXXXXXX.quality.json").write_text("{}", encoding="utf-8")
        (self.articles_dir / "2026-05-01-B0XXXXXXXX.enrichment.json").write_text("{}", encoding="utf-8")
        (self.articles_dir / "2026-05-01-B0XXXXXXXX.seo.json").write_text("{}", encoding="utf-8")
        out = F.discover_target_asins(self.articles_dir)
        self.assertEqual(list(out.keys()), ["B0XXXXXXXX"])

    def test_picks_newest_stem_for_duplicate_asin(self):
        _write_article(self.articles_dir, "2026-05-01-B0YYYYYYYY.json", "old name")
        _write_article(self.articles_dir, "2026-06-01-B0YYYYYYYY.json", "new name")
        out = F.discover_target_asins(self.articles_dir)
        self.assertEqual(out["B0YYYYYYYY"].name, "2026-06-01-B0YYYYYYYY.json")

    def test_ignores_files_without_parseable_asin(self):
        _write_article(self.articles_dir, "not-enough-parts.json")
        out = F.discover_target_asins(self.articles_dir)
        self.assertEqual(out, {})

    def test_missing_dir_returns_empty(self):
        out = F.discover_target_asins(self.articles_dir / "does-not-exist")
        self.assertEqual(out, {})


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "suggest"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_output_is_top_priority(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json")
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBBB.json")
        self.out_dir.mkdir()
        # B0BBBBBBBB already has a fresh-ish result; B0AAAAAAAA has none.
        (self.out_dir / "B0BBBBBBBB.json").write_text(
            json.dumps({"fetched_at": "2020-01-01T00:00:00Z"}), encoding="utf-8",
        )
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        targets = F.select_targets(self.articles_dir, self.out_dir, limit=40, min_age_days=30, now=now)
        asins = [a for a, _ in targets]
        self.assertEqual(asins[0], "B0AAAAAAAA", "missing-output ASIN must come first")
        self.assertIn("B0BBBBBBBB", asins)

    def test_fresh_output_excluded(self):
        _write_article(self.articles_dir, "2026-05-01-B0CCCCCCCC.json")
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "B0CCCCCCCC.json").write_text(
            json.dumps({"fetched_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
        targets = F.select_targets(self.articles_dir, self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual(targets, [])

    def test_stale_output_included_and_ordered_oldest_first(self):
        _write_article(self.articles_dir, "2026-05-01-B0DDDDDDDD.json")
        _write_article(self.articles_dir, "2026-05-01-B0EEEEEEEE.json")
        self.out_dir.mkdir()
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        (self.out_dir / "B0DDDDDDDD.json").write_text(
            json.dumps({"fetched_at": "2026-01-01T00:00:00Z"}), encoding="utf-8",
        )
        (self.out_dir / "B0EEEEEEEE.json").write_text(
            json.dumps({"fetched_at": "2026-02-01T00:00:00Z"}), encoding="utf-8",
        )
        targets = F.select_targets(self.articles_dir, self.out_dir, limit=40, min_age_days=30, now=now)
        self.assertEqual([a for a, _ in targets], ["B0DDDDDDDD", "B0EEEEEEEE"])

    def test_limit_caps_result(self):
        for i in range(5):
            _write_article(self.articles_dir, f"2026-05-01-B0FFFFFFF{i}.json")
        targets = F.select_targets(self.articles_dir, self.out_dir, limit=2, min_age_days=30)
        self.assertEqual(len(targets), 2)

    def test_corrupt_existing_output_treated_as_missing(self):
        _write_article(self.articles_dir, "2026-05-01-B0GGGGGGGG.json")
        self.out_dir.mkdir()
        (self.out_dir / "B0GGGGGGGG.json").write_text("{not json", encoding="utf-8")
        targets = F.select_targets(self.articles_dir, self.out_dir, limit=40, min_age_days=30)
        self.assertEqual([a for a, _ in targets], ["B0GGGGGGGG"])


class ExtractSuggestionsTest(unittest.TestCase):
    def test_flat_strings(self):
        out = F._extract_suggestions(["seed", ["a", "b", "c"]])
        self.assertEqual(out, ["a", "b", "c"])

    def test_nested_list_items_take_first_element(self):
        out = F._extract_suggestions(["seed", [["a", 0], ["b", 1]]])
        self.assertEqual(out, ["a", "b"])

    def test_unexpected_shape_raises(self):
        with self.assertRaises(F.SuggestParseError):
            F._extract_suggestions({"not": "a list"})
        with self.assertRaises(F.SuggestParseError):
            F._extract_suggestions(["only one element"])


class DedupeSuggestionsTest(unittest.TestCase):
    def test_dedupes_across_seeds_by_normalized_form(self):
        raw = [
            ("どこでもぺたクル 口コミ", "どこでもぺたクル"),
            ("ドコデモ ペタクル 口コミ", "どこでもぺたクル "),  # NFKC/lower-equivalent-ish diff casing only handled if same text
            ("どこでもぺたクル 評判", "どこでもぺたクル"),
        ]
        out = F.dedupe_suggestions(raw, ["どこでもぺたクル", "どこでもぺたクル "])
        queries = [d["query"] for d in out]
        self.assertIn("どこでもぺたクル 口コミ", queries)
        self.assertIn("どこでもぺたクル 評判", queries)

    def test_seed_itself_excluded(self):
        raw = [("どこでもぺたクル", "どこでもぺたクル"), ("どこでもぺたクル 口コミ", "どこでもぺたクル")]
        out = F.dedupe_suggestions(raw, ["どこでもぺたクル", "どこでもぺたクル "])
        queries = [d["query"] for d in out]
        self.assertNotIn("どこでもぺたクル", queries)
        self.assertEqual(queries, ["どこでもぺたクル 口コミ"])

    def test_exact_duplicate_normalized_removed(self):
        raw = [("ABC 口コミ", "seed1"), ("abc　口コミ", "seed2")]  # full-width space + case diff
        out = F.dedupe_suggestions(raw, ["seed1", "seed2"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["query"], "ABC 口コミ")  # original casing preserved (first-seen wins)


class RunHttpErrorAbortTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "suggest"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_200_aborts_after_first_success(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAA1.json", "商品A")
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAA2.json", "商品B")

        session = mock.Mock()
        # 商品A: both seeds succeed. 商品B: first seed returns 403.
        session.get.side_effect = [
            _resp(200, ["商品A", ["商品A サジェスト1"]]),
            _resp(200, ["商品A ", ["商品A サジェスト2"]]),
            _resp(403),
        ]
        summary = F.run(
            self.articles_dir, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 1)
        written_files = list(self.out_dir.glob("*.json"))
        self.assertEqual(len(written_files), 1)

    def test_connection_error_aborts(self):
        _write_article(self.articles_dir, "2026-05-01-B0BBBBBBB1.json", "商品C")
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError("boom")
        summary = F.run(
            self.articles_dir, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 0)


class RunParseFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "suggest"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_three_consecutive_parse_failures_abort(self):
        for i in range(4):
            _write_article(self.articles_dir, f"2026-05-01-B0CCCCCC0{i}.json", f"商品{i}")

        session = mock.Mock()
        # Every asin's first seed request returns unparsable JSON -> parse failure per asin.
        session.get.return_value = _resp(text="not json at all")
        summary = F.run(
            self.articles_dir, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["written"], 0)
        # aborted after 3rd consecutive failure -> at most 3 asins attempted
        self.assertLessEqual(session.get.call_count, 3)


class RunDryRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "suggest"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_does_not_call_network_or_write(self):
        _write_article(self.articles_dir, "2026-05-01-B0DDDDDDD1.json", "商品D")
        session = mock.Mock()
        summary = F.run(
            self.articles_dir, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, dry_run=True, session=session,
        )
        session.get.assert_not_called()
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["written"], 0)


class RunSkipsMissingProductNameTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "suggest"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_product_name_skips_without_network_call(self):
        p = self.articles_dir / "2026-05-01-B0EEEEEEE1.json"
        p.write_text(json.dumps({"product": {}}), encoding="utf-8")
        session = mock.Mock()
        summary = F.run(
            self.articles_dir, self.out_dir, limit=40, min_age_days=30,
            sleep_min=0, sleep_max=0, session=session, sleeper=lambda _s: None,
        )
        session.get.assert_not_called()
        self.assertEqual(summary["skipped_no_name"], 1)
        self.assertEqual(summary["written"], 0)
        self.assertFalse(summary["aborted"])


class WriteResultTest(unittest.TestCase):
    def test_output_shape(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = pathlib.Path(td) / "suggest"
            now = datetime(2026, 7, 12, 12, 34, 56, tzinfo=timezone.utc)
            path = F.write_result(
                out_dir, "B0ZZZZZZZZ", "テスト商品",
                ["テスト商品", "テスト商品 "],
                [{"query": "テスト商品 口コミ", "seed": "テスト商品"}],
                now=now,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["asin"], "B0ZZZZZZZZ")
            self.assertEqual(data["fetched_at"], "2026-07-12T12:34:56Z")
            self.assertEqual(data["suggestions"][0]["query"], "テスト商品 口コミ")


if __name__ == "__main__":
    unittest.main()
