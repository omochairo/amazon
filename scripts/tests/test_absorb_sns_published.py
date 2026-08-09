"""Unit tests for #4765 水平展開 — sns-publish 側の配信済み ASIN 取り込み。

再投稿防止が成り立つ条件:
  1. 滞留ブランチ側にしか無い ASIN が必ず取り込まれること
     (取り込まれないと次の cron が同じ記事リンクを再投稿する)。
  2. target 側の既存履歴を消したり並べ替えたりしないこと
     (published は配信順の履歴で、上限超過時に古い側から削られるため)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from absorb_sns_published import absorb, main  # type: ignore[import-not-found]


class AbsorbTests(unittest.TestCase):
    def test_missing_asin_is_absorbed(self):
        out, added = absorb(["A1", "A2"], ["A2", "A3"])
        self.assertEqual(added, ["A3"])
        self.assertEqual(out, ["A1", "A2", "A3"])

    def test_existing_order_is_preserved_and_nothing_removed(self):
        out, added = absorb(["A1", "A2", "A3"], ["A3", "A1"])
        self.assertEqual(added, [])
        self.assertEqual(out, ["A1", "A2", "A3"])

    def test_asin_is_normalized_to_upper(self):
        out, added = absorb(["A1"], [" b0abc123 "])
        self.assertEqual(added, ["B0ABC123"])
        self.assertEqual(out, ["A1", "B0ABC123"])

    def test_duplicate_in_source_added_once(self):
        out, added = absorb([], ["A9", "A9"])
        self.assertEqual(added, ["A9"])
        self.assertEqual(out, ["A9"])

    def test_non_string_entries_are_skipped(self):
        out, added = absorb(["A1"], [None, 42, {"asin": "A2"}, "A3"])
        self.assertEqual(added, ["A3"])
        self.assertEqual(out, ["A1", "A3"])

    def test_limit_trims_oldest_like_pick_sns_target(self):
        out, added = absorb(["A1", "A2"], ["A3"], limit=2)
        self.assertEqual(added, ["A3"])
        self.assertEqual(out, ["A2", "A3"])

    def test_no_limit_keeps_everything(self):
        out, _ = absorb(["A1", "A2"], ["A3"], limit=None)
        self.assertEqual(out, ["A1", "A2", "A3"])


class CliTests(unittest.TestCase):
    def _write(self, path, published):
        path.write_text(json.dumps({"published": published, "updated": None},
                                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    def test_cli_absorbs_and_writes(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target, source = d / "state.json", d / "stale.json"
            self._write(target, ["A1"])
            self._write(source, ["A1", "A2"])

            sys.argv = ["absorb", "--target", str(target), "--source", str(source)]
            self.assertEqual(main(), 0)

            got = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(got["published"], ["A1", "A2"])
            self.assertIsNotNone(got["updated"])

    def test_cli_dry_run_leaves_target_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target, source = d / "state.json", d / "stale.json"
            self._write(target, ["A1"])
            self._write(source, ["A2"])
            before = target.read_text(encoding="utf-8")

            sys.argv = ["absorb", "--target", str(target), "--source", str(source), "--dry-run"]
            self.assertEqual(main(), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_cli_malformed_source_is_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target, source = d / "state.json", d / "stale.json"
            self._write(target, ["A1"])
            source.write_text("{not json", encoding="utf-8")
            before = target.read_text(encoding="utf-8")

            sys.argv = ["absorb", "--target", str(target), "--source", str(source)]
            self.assertEqual(main(), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_cli_missing_source_is_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target = d / "state.json"
            self._write(target, ["A1"])
            sys.argv = ["absorb", "--target", str(target), "--source", str(d / "nope.json")]
            self.assertEqual(main(), 0)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["published"], ["A1"])


if __name__ == "__main__":
    unittest.main()
