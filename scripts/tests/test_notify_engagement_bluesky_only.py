"""Unit tests for #10628 — X を休むスロットで X の queue を Bluesky だけに流す。

守りたいこと:
  1. bluesky channel は X (Buffer) を一切呼ばず、Bluesky に投稿して row を配信済みにする
     (配信済みにしないと X の在庫が補充との差で溜まり続ける)。
  2. creds 未設定なら何もせず row を pending のまま残す (従来の mirror と同じ inert)。
  3. Bluesky が失敗したら row は pending のまま、戻り値 False で run を赤にする。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import notify_engagement as ne  # type: ignore[import-not-found]  # noqa: E402

CREDS = {"BLUESKY_IDENTIFIER": "example.bsky.social", "BLUESKY_APP_PASSWORD": "dummy"}


def _row(rid: str) -> dict:
    return {
        "id": rid,
        "channel": "x",
        "category": "daily",
        "subcategory": "tips",
        "text": "テスト本文",
        "created_at": "2026-10-01T00:00:00+00:00",
        "published_at": None,
        "post_id": None,
    }


class BlueskyOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = pathlib.Path(self.tmp.name) / "engagement_queue_x.jsonl"
        self.queue.write_text(json.dumps(_row("e-1"), ensure_ascii=False) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self) -> list[dict]:
        return [json.loads(l) for l in self.queue.read_text(encoding="utf-8").splitlines() if l]

    def _consume(self, **kw) -> bool:
        return ne.consume_one(self.queue, "bluesky", dry_run=kw.get("dry_run", False),
                              live=True, category="daily")

    def test_posts_to_bluesky_only_and_marks_published(self):
        with mock.patch.dict(os.environ, CREDS), \
             mock.patch.object(ne, "post_bluesky_text", return_value=(True, "at://did/post/1")) as bs, \
             mock.patch.object(ne, "post_x_via_buffer") as x:
            self.assertTrue(self._consume())
        bs.assert_called_once_with("テスト本文")
        x.assert_not_called()
        row = self._rows()[0]
        self.assertIsNotNone(row["published_at"])
        self.assertIsNone(row["post_id"])
        self.assertEqual(row["bluesky_post_id"], "at://did/post/1")

    def test_no_creds_is_inert(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("BLUESKY_IDENTIFIER", "BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ne, "post_bluesky_text") as bs:
            self.assertTrue(self._consume())
        bs.assert_not_called()
        self.assertIsNone(self._rows()[0]["published_at"])

    def test_failure_keeps_row_pending(self):
        with mock.patch.dict(os.environ, CREDS), \
             mock.patch.object(ne, "post_bluesky_text", return_value=(False, None)):
            self.assertFalse(self._consume())
        self.assertIsNone(self._rows()[0]["published_at"])

    def test_dry_run_does_not_post_or_mutate(self):
        with mock.patch.dict(os.environ, CREDS), \
             mock.patch.object(ne, "post_bluesky_text") as bs:
            self.assertTrue(self._consume(dry_run=True))
        bs.assert_not_called()
        self.assertIsNone(self._rows()[0]["published_at"])


class MainChannelRoutingTest(unittest.TestCase):
    def _run_main(self, channels: str) -> list[tuple]:
        calls: list[tuple] = []

        def fake(queue_path, channel, *a, **kw):
            calls.append((queue_path, channel))
            return True

        with mock.patch.object(ne, "consume_one", side_effect=fake), \
             mock.patch.object(sys, "argv", ["notify_engagement.py", "--channels", channels,
                                             "--category", "daily", "--bluesky-mirror", "--dry-run"]):
            self.assertEqual(ne.main(), 0)
        return calls

    def test_bluesky_threads_uses_x_queue_without_x(self):
        calls = self._run_main("bluesky,threads")
        self.assertEqual(calls, [(ne.QUEUE_X, "bluesky"), (ne.QUEUE_THREADS, "threads")])

    def test_x_threads_unchanged(self):
        calls = self._run_main("x,threads")
        self.assertEqual(calls, [(ne.QUEUE_X, "x"), (ne.QUEUE_THREADS, "threads")])


if __name__ == "__main__":
    unittest.main()
