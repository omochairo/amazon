"""Unit tests for #10628 — 配信済み engagement 投稿の反応の取得と記録用 Markdown。

外部 API は叩かない。守りたいこと:
  1. 対象は JST の日付で区切る (schedule が数時間遅れても取りこぼし・二重計測しない)。
  2. Bluesky だけに流した row (post_id null) は X として測らない。
  3. 値は --out のファイルにだけ書き、標準出力に出さない (Actions のログは public)。
  4. token 未設定は黙って緑にしない。取得失敗は行を残したうえで run を赤にする。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import fetch_engagement_metrics as fem  # type: ignore[import-not-found]  # noqa: E402

JST = timezone(timedelta(hours=9))


def _row(rid, published_jst: str | None, post_id="p", **kw):
    at = None
    if published_jst:
        at = datetime.fromisoformat(published_jst).replace(tzinfo=JST).astimezone(timezone.utc).isoformat()
    return {"id": rid, "post_id": post_id, "published_at": at, "category": "daily",
            "subcategory": "vent", "text": kw.get("text", "本文"), }


class SelectTest(unittest.TestCase):
    def test_jst_date_boundary(self):
        rows = [
            _row("before", "2026-10-24T23:59:00"),
            _row("first", "2026-10-25T00:00:00"),
            _row("late", "2026-10-25T23:50:00"),   # UTC では 10-25 14:50
            _row("early", "2026-10-25T08:00:00"),  # UTC では 10-24 23:00
            _row("after", "2026-10-26T00:00:00"),
            _row("bluesky-only", "2026-10-25T12:00:00", post_id=None),
            _row("unpublished", None),
        ]
        got = [r["id"] for r in fem.select_rows(rows, date(2026, 10, 25))]
        self.assertEqual(got, ["first", "early", "late"])


class ParseTest(unittest.TestCase):
    def test_buffer_values_mapped(self):
        payload = {"data": {"post": {"metricsUpdatedAt": "2026-10-27T00:00:00Z", "metrics": [
            {"type": "impressions", "value": 120.0}, {"type": "reactions", "value": 3.0},
            {"type": "engagementRate", "value": 2.5}, {"type": "unknownType", "value": 9.0}]}}}
        with mock.patch.object(fem, "_http", return_value=(200, payload)):
            values, updated = fem.fetch_buffer_metrics("p1", "t")
        self.assertEqual(values, {"表示": 120.0, "いいね": 3.0, "反応率%": 2.5})
        self.assertEqual(updated, "2026-10-27T00:00:00Z")

    def test_buffer_scope_error(self):
        payload = {"errors": [{"message": "x", "extensions": {"code": "INSUFFICIENT_SCOPE"}}]}
        with mock.patch.object(fem, "_http", return_value=(200, payload)):
            self.assertEqual(fem.fetch_buffer_metrics("p1", "t"), (None, "errors=INSUFFICIENT_SCOPE"))

    def test_threads_values_and_total_value(self):
        payload = {"data": [
            {"name": "views", "values": [{"value": 300}]},
            {"name": "likes", "total_value": {"value": 7}},
            {"name": "replies", "values": [{"value": 1}]}]}
        with mock.patch.object(fem, "_http", return_value=(200, payload)):
            values, _ = fem.fetch_threads_insights("m1", "t")
        self.assertEqual(values, {"表示": 300, "いいね": 7, "返信": 1})

    def test_threads_denied(self):
        with mock.patch.object(fem, "_http", return_value=(400, {"error": {"code": 10}})):
            self.assertEqual(fem.fetch_threads_insights("m1", "t"), (None, "http=400 code=10"))


class MarkdownTest(unittest.TestCase):
    def test_table_and_json_block(self):
        recs = [
            {"channel": "X", "id": "e1", "post_id": "p1", "published_jst": "16:26", "category": "news",
             "subcategory": "台風", "text": "a|b " + "長" * 60, "metrics": {"表示": 120.0, "反応率%": 2.5},
             "note": ""},
            {"channel": "Threads", "id": "e2", "post_id": "m1", "published_jst": "14:10", "category": "daily",
             "subcategory": None, "text": "t", "metrics": None, "note": "http=400"},
        ]
        md = fem.build_markdown(date(2026, 10, 25), recs, datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc))
        self.assertIn("## 2026-10-25（JST）に配信した分", md)
        self.assertIn("| X | 16:26 | news/台風 | a｜b ", md)   # 表を壊す | を置換
        self.assertIn("| 120 | - | - | - | - | - | - | 2.5 |", md)
        self.assertIn("| Threads | 14:10 | daily/- | t | 取得失敗 |", md)
        blob = md.split("<!-- engagement-metrics-json\n", 1)[1].split("\n-->", 1)[0]
        self.assertEqual(json.loads(blob)["records"][0]["metrics"]["表示"], 120.0)

    def test_empty(self):
        md = fem.build_markdown(date(2026, 10, 25), [], datetime(2026, 10, 28, tzinfo=timezone.utc))
        self.assertIn("対象の投稿なし", md)


class MainTest(unittest.TestCase):
    def _run(self, env, records):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "m.md"
            buf = io.StringIO()
            with mock.patch.dict("os.environ", env, clear=True), \
                 mock.patch.object(fem, "collect", return_value=records), redirect_stdout(buf):
                code = fem.main(["--out", str(out)])
            return code, buf.getvalue(), out.read_text(encoding="utf-8") if out.exists() else ""

    def test_missing_token_is_red(self):
        code, stdout, _ = self._run({"BUFFER_ACCESS_TOKEN": "t"}, [])
        self.assertEqual(code, 2)
        self.assertIn("THREADS_ACCESS_TOKEN", stdout)

    def test_values_only_in_file(self):
        rec = {"channel": "X", "id": "e1", "post_id": "p1", "published_jst": "16:26", "category": "news",
               "subcategory": "x", "text": "本文", "metrics": {"表示": 98765.0}, "note": ""}
        code, stdout, md = self._run({"BUFFER_ACCESS_TOKEN": "t", "THREADS_ACCESS_TOKEN": "t"}, [rec])
        self.assertEqual(code, 0)
        self.assertIn("98765", md)
        self.assertNotIn("98765", stdout)
        self.assertNotIn("本文", stdout)

    def test_failure_is_red_but_written(self):
        rec = {"channel": "Threads", "id": "e2", "post_id": "m1", "published_jst": "14:10", "category": "daily",
               "subcategory": "x", "text": "t", "metrics": None, "note": "http=400"}
        code, stdout, md = self._run({"BUFFER_ACCESS_TOKEN": "t", "THREADS_ACCESS_TOKEN": "t"}, [rec])
        self.assertEqual(code, 1)
        self.assertIn("取得失敗", md)
        self.assertIn("::warning::[Threads] 取得失敗 (http=400)", stdout)


class ArticleSourceTest(unittest.TestCase):
    """navi-brain#139 — 記事投稿 (sns_published.json の x_posts) も X として測る。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self._tmp.name)
        self.qx, self.qth, self.sp = d / "qx.jsonl", d / "qth.jsonl", d / "sp.json"
        self.qx.write_text(json.dumps(_row("eng", "2026-10-25T14:00:00", post_id="e1")) + "\n",
                           encoding="utf-8")
        self.qth.write_text(json.dumps(_row("th", "2026-10-25T14:00:00", post_id="t1")) + "\n",
                            encoding="utf-8")
        at = datetime(2026, 10, 25, 23, 30, tzinfo=JST).astimezone(timezone.utc).isoformat()
        self.sp.write_text(json.dumps({"published": ["B0ART00001"], "x_posts": {
            "B0ART00001": {"post_id": "a1", "published_at": at, "text": "記事\n\n→ https://x"},
        }}), encoding="utf-8")
        self._patches = [mock.patch.object(fem, "QUEUE_X", self.qx),
                         mock.patch.object(fem, "QUEUE_THREADS", self.qth),
                         mock.patch.object(fem, "SNS_PUBLISHED", self.sp),
                         mock.patch.object(fem, "fetch_buffer_metrics", return_value=({"表示": 1}, "")),
                         mock.patch.object(fem, "fetch_threads_insights", return_value=({"表示": 2}, ""))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _ids(self, source):
        return [(r["channel"], r["id"]) for r in fem.collect(date(2026, 10, 25), "x", "t", source)]

    def test_all_includes_article_in_time_order(self):
        self.assertEqual(self._ids("all"),
                         [("X", "eng"), ("X", "article-x-B0ART00001"), ("Threads", "th")])

    def test_article_only_skips_engagement_rows(self):
        self.assertEqual(self._ids("article"), [("X", "article-x-B0ART00001")])

    def test_engagement_only_matches_old_behaviour(self):
        self.assertEqual(self._ids("engagement"), [("X", "eng"), ("Threads", "th")])

    def test_article_row_shape(self):
        rec = [r for r in fem.collect(date(2026, 10, 25), "x", "t", "article")][0]
        self.assertEqual((rec["category"], rec["subcategory"], rec["post_id"], rec["published_jst"]),
                         ("article", "B0ART00001", "a1", "23:30"))

    def test_missing_or_broken_state_is_empty(self):
        self.sp.write_text("{broken", encoding="utf-8")
        self.assertEqual(self._ids("article"), [])
        self.sp.unlink()
        self.assertEqual(self._ids("article"), [])

    def test_article_only_does_not_need_threads_token(self):
        out = pathlib.Path(self._tmp.name) / "out.md"
        with mock.patch.dict("os.environ", {"BUFFER_ACCESS_TOKEN": "x", "THREADS_ACCESS_TOKEN": ""}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(fem.main(["--out", str(out), "--source", "article"]), 0)
            self.assertEqual(fem.main(["--out", str(out), "--source", "all"]), 2)

    def test_heading_marks_article_only_runs(self):
        md = fem.build_markdown(date(2026, 10, 25), [], datetime(2026, 10, 28, tzinfo=timezone.utc), "article")
        self.assertTrue(md.startswith("## 2026-10-25（JST）に配信した分（記事投稿のみ）"))
        md = fem.build_markdown(date(2026, 10, 25), [], datetime(2026, 10, 28, tzinfo=timezone.utc))
        self.assertTrue(md.startswith("## 2026-10-25（JST）に配信した分\n"))


if __name__ == "__main__":
    unittest.main()
