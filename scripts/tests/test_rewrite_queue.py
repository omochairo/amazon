"""Invariant tests for rewrite_queue.py (Issue #2711).

The bug being prevented: idle-fill deleted the body to request a rewrite, so a
silently-failed regeneration left the page 404 (~338 bodies lost). The redesign
decouples "covered" from "needs rewrite" via a marker, and only ever deletes the
old body AFTER a newer one exists. These tests pin the load-bearing invariants:

* FAILED regeneration (no newer body) -> old body is NEVER removed, ASIN stays
  eligible for a retry.
* SUCCESSFUL regeneration (newer body landed) -> old body + marker are removed,
  ASIN drops out of the eligible set (no infinite re-pick).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import rewrite_queue as rq  # noqa: E402

ASIN = "B00I7JXEEA"


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{}")


class MarkerIoTest(unittest.TestCase):
    def test_write_keeps_original_timestamp_on_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rq.write_marker(ASIN, "2026-05-01-" + ASIN, d, requested_at="2026-05-01T00:00:00Z")
            rq.write_marker(ASIN, "2026-05-01-" + ASIN, d)  # idempotent retry
            with open(rq.marker_path(ASIN, d), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["requested_at"], "2026-05-01T00:00:00Z")

    def test_rejects_bad_asin(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                rq.write_marker("not-an-asin", "x", d)

    def test_newest_body_slug(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _touch(os.path.join(d, f"2026-05-01-{ASIN}.json"))
            _touch(os.path.join(d, f"2026-05-09-{ASIN}.json"))
            _touch(os.path.join(d, f"2026-05-09-{ASIN}.quality.json"))  # sidecar ignored
            self.assertEqual(rq.newest_body_slug(ASIN, d), f"2026-05-09-{ASIN}")
            self.assertIsNone(rq.newest_body_slug("B0EMPTY0000", d))


class FailedRegenInvariantTest(unittest.TestCase):
    """No newer body -> body is kept and ASIN stays eligible (retry)."""

    def test_body_survives_and_stays_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            articles = os.path.join(base, "articles")
            queue = os.path.join(base, "queue")
            old_slug = f"2026-05-01-{ASIN}"
            old_body = os.path.join(articles, f"{old_slug}.json")
            _touch(old_body)
            rq.write_marker(ASIN, old_slug, queue)
            # cleanup must NOT touch the body (no replacement exists yet).
            files, markers = rq.cleanup_completed(articles, queue)
            self.assertEqual((files, markers), (0, 0))
            self.assertTrue(os.path.exists(old_body), "body must never be lost")
            self.assertTrue(os.path.exists(rq.marker_path(ASIN, queue)))
            # Still eligible so 03-invoke-jules retries it.
            self.assertIn(ASIN, rq.eligible_rewrite_asins(articles, queue))


class SuccessfulRegenInvariantTest(unittest.TestCase):
    """Newer body landed -> swap: old body + marker removed, ASIN drops out."""

    def test_swap_removes_old_and_clears_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            articles = os.path.join(base, "articles")
            queue = os.path.join(base, "queue")
            old_slug = f"2026-05-01-{ASIN}"
            new_slug = f"2026-05-20-{ASIN}"
            old_body = os.path.join(articles, f"{old_slug}.json")
            new_body = os.path.join(articles, f"{new_slug}.json")
            _touch(old_body)
            rq.write_marker(ASIN, old_slug, queue)
            # Replacement lands (Jules PR merged).
            _touch(new_body)
            # Already NOT eligible once the newer body exists (self-healing,
            # even before cleanup runs) -> no infinite re-pick.
            self.assertNotIn(ASIN, rq.eligible_rewrite_asins(articles, queue))
            files, markers = rq.cleanup_completed(articles, queue)
            self.assertEqual(markers, 1)
            self.assertGreaterEqual(files, 1)
            self.assertFalse(os.path.exists(old_body))
            self.assertTrue(os.path.exists(new_body), "replacement must survive")
            self.assertFalse(os.path.exists(rq.marker_path(ASIN, queue)))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# #5490: 生成側が defer するマーカーの取り下げ
# ---------------------------------------------------------------------------

class WithdrawDeferredTest(unittest.TestCase):
    """`cleanup_completed` は新しい本体が着地したときだけ消すので、**生成されようが
    無い ASIN のマーカーは永久に残る**。実測 (2026-08-18) で 11 件が band=zero のまま
    滞留し、最古は 56 日だった。
    """

    def _queue(self, tmp: str, *asins: str) -> str:
        qdir = os.path.join(tmp, "queue")
        for a in asins:
            rq.write_marker(a, f"2026-05-01-{a}", qdir)
        return qdir

    def test_withdraws_only_deferred_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qdir = self._queue(tmp, "B0AAAAAAAA", "B0BBBBBBBB")
            with mock.patch.object(rq, "is_generatable", lambda a: a != "B0AAAAAAAA"):
                withdrawn = rq.withdraw_deferred(qdir)
            self.assertEqual(withdrawn, ["B0AAAAAAAA"])
            self.assertFalse(os.path.exists(rq.marker_path("B0AAAAAAAA", qdir)))
            self.assertTrue(os.path.exists(rq.marker_path("B0BBBBBBBB", qdir)))

    def test_withdrawing_does_not_touch_article_bodies(self) -> None:
        """取り下げても本体は消さない。

        リライトを諦めるだけで記事は今のまま配信され続ける。旧本体を消してよいのは
        置き換えが着地したときだけ、という #2711 の不変条件を壊さないこと。
        """
        with tempfile.TemporaryDirectory() as tmp:
            qdir = self._queue(tmp, "B0AAAAAAAA")
            adir = os.path.join(tmp, "articles")
            os.makedirs(adir)
            body = os.path.join(adir, "2026-05-01-B0AAAAAAAA.json")
            with open(body, "w", encoding="utf-8") as f:
                f.write("{}")
            with mock.patch.object(rq, "is_generatable", lambda a: False):
                rq.withdraw_deferred(qdir)
            self.assertTrue(os.path.exists(body), "本体が消えてはいけない")

    def test_nothing_to_withdraw_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qdir = self._queue(tmp, "B0AAAAAAAA")
            with mock.patch.object(rq, "is_generatable", lambda a: True):
                self.assertEqual(rq.withdraw_deferred(qdir), [])
            self.assertTrue(os.path.exists(rq.marker_path("B0AAAAAAAA", qdir)))

    def test_is_generatable_is_permissive_when_scoring_fails(self) -> None:
        """判定できないことを理由に候補を減らさない (生成側の defer が独立に効く)。"""
        with mock.patch.dict("sys.modules", {"score_per_asin_info": None}):
            self.assertTrue(rq.is_generatable("B0AAAAAAAA"))
