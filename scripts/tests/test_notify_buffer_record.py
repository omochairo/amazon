"""navi-brain#139 — notify_buffer.py が記事投稿の X 投稿 ID を書き出す。

外部 API は叩かない。守りたいこと:
  1. X に出せた (--live で PostActionSuccess) ときだけ書き出す。下書き・失敗では書かない。
  2. 書き出しに失敗しても exit を非ゼロにしない (投稿済みの X が failure と記録されるため)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import notify_buffer as nb  # type: ignore[import-not-found]  # noqa: E402

OK = {"data": {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "pid1"}}}}
LIMIT = {"data": {"createPost": {"__typename": "LimitReachedError", "message": "x"}}}


class PostedIdTest(unittest.TestCase):
    def test_success(self):
        self.assertEqual(nb.posted_id(OK), "pid1")

    def test_failures(self):
        for result in (LIMIT, {"errors": [{"message": "x"}]}, {},
                       {"data": {"createPost": {"__typename": "PostActionSuccess", "post": {}}}}):
            with self.subTest(result=result):
                self.assertIsNone(nb.posted_id(result))


class MainRecordTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self._tmp.name) / "x_post.json"
        self._argv = sys.argv
        self._patches = [
            mock.patch.dict("os.environ", {"BUFFER_ACCESS_TOKEN": "t"}),
            mock.patch.object(nb, "load_article", return_value={"slug": "a-B0ART00001", "title": "題"}),
            mock.patch.object(nb.sns_copy_store, "get_hook", return_value="フック"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        sys.argv = self._argv
        self._tmp.cleanup()

    def _main(self, result, *extra):
        sys.argv = ["notify_buffer.py", "B0ART00001", "--x-only", *extra, "--record-x-post", str(self.out)]
        with mock.patch.object(nb, "create_post", return_value=result):
            return nb.main()

    def test_live_success_writes_record(self):
        self.assertEqual(self._main(OK, "--live"), 0)
        rec = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(rec["post_id"], "pid1")
        self.assertTrue(rec["text"].startswith("フック\n\n→ https://navi.omcha.jp/products/b0art00001/"))
        self.assertTrue(rec["published_at"].endswith("+00:00"))

    def test_draft_does_not_write(self):
        self.assertEqual(self._main(OK), 0)
        self.assertFalse(self.out.exists())

    def test_failure_does_not_write(self):
        self.assertEqual(self._main(LIMIT, "--live"), 1)
        self.assertFalse(self.out.exists())

    def test_write_error_keeps_exit_zero(self):
        with mock.patch.object(nb, "write_x_post_record", side_effect=OSError("disk")):
            self.assertEqual(self._main(OK, "--live"), 0)


if __name__ == "__main__":
    unittest.main()
