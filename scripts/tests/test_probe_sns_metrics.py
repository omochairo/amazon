"""Unit tests for #10628 — 投稿ごとの反応を読めるかの probe。

外部 API は叩かない。守りたいのは 2 点:
  1. 対象に選ぶのは配信から 48h〜10 日の row だけ (Buffer の metrics は日次取り込みなので、
     配信直後の post を見ると「取れない」と誤判定する)。
  2. 出力に metric の**値**を一切含めない (Actions のログは public)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import probe_sns_inbox as probe  # type: ignore[import-not-found]  # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _write(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(pid, hours_ago):
    at = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds") if hours_ago is not None else None
    return {"id": f"e-{pid}", "post_id": pid, "published_at": at}


class PickTest(unittest.TestCase):
    def test_window_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "q.jsonl"
            _write(p, [
                _row("too-new", 24),
                _row("a", 50),
                _row("b", 100),
                _row("c", 72),
                _row("too-old", 24 * 11),
                _row(None, 60),          # Bluesky のみ配信した row (post_id null)
                _row("unpublished", None),
            ])
            self.assertEqual(probe.pick_published_post_ids(p, NOW), ["a", "c", "b"])
            self.assertEqual(probe.pick_published_post_ids(p, NOW, n=1), ["a"])

    def test_missing_file(self):
        self.assertEqual(probe.pick_published_post_ids(pathlib.Path("no-such.jsonl"), NOW), [])


class BufferMetricsTest(unittest.TestCase):
    def _run(self, payload):
        with mock.patch.dict("os.environ", {"BUFFER_ACCESS_TOKEN": "t"}), \
             mock.patch.object(probe, "pick_published_post_ids", return_value=["p1", "p2"]), \
             mock.patch.object(probe, "_post_json", return_value=(200, payload, "")):
            return probe.probe_buffer_metrics()

    def test_impressions_detected_without_values(self):
        payload = {"data": {"post": {"id": "p1", "metricsUpdatedAt": "2026-10-27T00:00:00Z",
                                     "metrics": [{"type": "impressions", "unit": "count", "value": 1234.0},
                                                 {"type": "reactions", "unit": "count", "value": 5.0}]}}}
        r = self._run(payload)
        self.assertEqual(r["status"], "impressions-ok")
        self.assertEqual(r["metric_types_seen"], ["impressions", "reactions"])
        self.assertNotIn("1234", json.dumps(r))

    def test_no_impressions(self):
        payload = {"data": {"post": {"id": "p1", "metricsUpdatedAt": None,
                                     "metrics": [{"type": "reactions", "unit": "count"}]}}}
        self.assertEqual(self._run(payload)["status"], "metrics-ok-no-impressions")

    def test_denied_reports_codes_only(self):
        payload = {"errors": [{"message": "secret detail", "extensions": {"code": "FORBIDDEN"}}]}
        r = self._run(payload)
        self.assertEqual(r["status"], "metrics-denied")
        self.assertEqual(r["posts"][0]["error_codes"], ["FORBIDDEN"])
        self.assertNotIn("secret detail", json.dumps(r))


class ThreadsInsightsTest(unittest.TestCase):
    def _run(self, status, payload):
        with mock.patch.dict("os.environ", {"THREADS_ACCESS_TOKEN": "t"}), \
             mock.patch.object(probe, "pick_published_post_ids", return_value=["m1"]), \
             mock.patch.object(probe, "_get_json", return_value=(status, payload, "")):
            return probe.probe_threads_insights()

    def test_ok_names_only(self):
        payload = {"data": [{"name": "likes", "values": [{"value": 987}]},
                            {"name": "views", "values": [{"value": 4321}]}]}
        r = self._run(200, payload)
        self.assertEqual(r["status"], "insights-ok")
        self.assertEqual(r["metric_names"], ["likes", "views"])
        self.assertNotIn("4321", json.dumps(r))

    def test_denied(self):
        r = self._run(400, {"error": {"type": "OAuthException", "code": 10, "message": "x"}})
        self.assertEqual(r["status"], "insights-denied")
        self.assertIn("code=10", r["reason"])


if __name__ == "__main__":
    unittest.main()
