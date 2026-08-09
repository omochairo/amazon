"""Unit tests for #4765 — 未マージの publish PR が持つ配信済みマーカーの取り込み。

重複投稿の再発防止が成り立つ条件は 2 つある:
  1. 滞留ブランチ側で published_at が付いた row は、必ず取り込まれること
     (取り込まれないと次スロットが同じ本文を再選定して二重投稿する)。
  2. main 側で既に published_at が付いている row は、絶対に上書きされないこと
     (滞留 PR の古い post_id で main の新しい値を潰すと、実際の投稿記録が失われる)。
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

from absorb_engagement_bookkeeping import absorb, main  # type: ignore[import-not-found]


def _row(rid, published_at=None, post_id=None, **extra):
    r = {"id": rid, "channel": "x", "text": "t", "published_at": published_at, "post_id": post_id}
    r.update(extra)
    return r


class AbsorbTests(unittest.TestCase):
    def test_pending_row_absorbs_marker(self):
        target = [_row("a"), _row("b")]
        rows, absorbed = absorb(target, [_row("b", "2026-08-08T02:30:17+00:00", "18090026066136623")])
        self.assertEqual(absorbed, ["b"])
        self.assertEqual(rows[1]["published_at"], "2026-08-08T02:30:17+00:00")
        self.assertEqual(rows[1]["post_id"], "18090026066136623")
        # 無関係な row は触らない
        self.assertIsNone(rows[0]["published_at"])

    def test_already_published_target_is_never_overwritten(self):
        """main 側が新しい post_id を持つとき、滞留 PR の古い値で潰さない。"""
        target = [_row("b", "2026-08-08T06:55:53+00:00", "18070517177444677")]
        rows, absorbed = absorb(target, [_row("b", "2026-08-08T02:30:17+00:00", "18090026066136623")])
        self.assertEqual(absorbed, [])
        self.assertEqual(rows[0]["post_id"], "18070517177444677")

    def test_bluesky_post_id_is_carried(self):
        target = [_row("x1")]
        src = _row("x1", "2026-08-08T02:29:32+00:00", "6a76948beb086b1202d0144a",
                   bluesky_post_id="at://did:plc:example/app.bsky.feed.post/3msjzm5icfb23")
        rows, absorbed = absorb(target, [src])
        self.assertEqual(absorbed, ["x1"])
        self.assertEqual(rows[0]["bluesky_post_id"],
                         "at://did:plc:example/app.bsky.feed.post/3msjzm5icfb23")

    def test_source_row_without_published_at_is_ignored(self):
        target = [_row("a")]
        rows, absorbed = absorb(target, [_row("a")])
        self.assertEqual(absorbed, [])
        self.assertIsNone(rows[0]["published_at"])

    def test_unknown_id_in_source_is_skipped(self):
        target = [_row("a")]
        rows, absorbed = absorb(target, [_row("ghost", "2026-08-08T02:30:17+00:00", "1")])
        self.assertEqual(absorbed, [])
        self.assertEqual(len(rows), 1)

    def test_multiple_sources_accumulate(self):
        target = [_row("a"), _row("b")]
        rows, absorbed = absorb(target, [_row("a", "2026-08-08T00:00:00+00:00", "1")])
        rows, absorbed2 = absorb(rows, [_row("b", "2026-08-08T01:00:00+00:00", "2")])
        self.assertEqual(absorbed + absorbed2, ["a", "b"])


class CliTests(unittest.TestCase):
    def _write(self, path, rows):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")

    def test_cli_writes_absorbed_target(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target, source = d / "queue.jsonl", d / "stale.jsonl"
            self._write(target, [_row("a"), _row("b")])
            self._write(source, [_row("b", "2026-08-08T02:30:17+00:00", "18090026066136623")])

            sys.argv = ["absorb", "--target", str(target), "--source", str(source)]
            self.assertEqual(main(), 0)

            rows = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(rows[1]["post_id"], "18090026066136623")

    def test_cli_dry_run_leaves_target_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target, source = d / "queue.jsonl", d / "stale.jsonl"
            self._write(target, [_row("b")])
            self._write(source, [_row("b", "2026-08-08T02:30:17+00:00", "1")])
            before = target.read_text(encoding="utf-8")

            sys.argv = ["absorb", "--target", str(target), "--source", str(source), "--dry-run"]
            self.assertEqual(main(), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_cli_missing_paths_are_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            target = d / "queue.jsonl"
            self._write(target, [_row("a")])
            sys.argv = ["absorb", "--target", str(target), "--source", str(d / "nope.jsonl")]
            self.assertEqual(main(), 0)

            sys.argv = ["absorb", "--target", str(d / "nope.jsonl")]
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
