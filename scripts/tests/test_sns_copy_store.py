"""sns_copy_store.py と notify_* の store 統合テスト (#1580 Part 2)。

実行: amazon-clone 直下から `python -m unittest scripts.tests.test_sns_copy_store`
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sns_copy_store as store  # noqa: E402
import notify_buffer  # noqa: E402
import notify_threads  # noqa: E402


class StoreTempDir:
    """STORE_DIR を一時 dir に差し替える context manager。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = store.STORE_DIR
        store.STORE_DIR = self.dir
        return self

    def write(self, asin, payload):
        (self.dir / f"{asin}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def __exit__(self, *exc):
        store.STORE_DIR = self._orig
        self._tmp.cleanup()


class LoadCopyTest(unittest.TestCase):
    def test_missing_returns_none(self):
        with StoreTempDir():
            self.assertIsNone(store.load_copy("B00TESTXYZ"))

    def test_invalid_asin_returns_none(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "hi"})
            self.assertIsNone(store.load_copy("not-an-asin"))
            self.assertIsNone(store.load_copy(""))
            self.assertIsNone(store.load_copy(None))

    def test_case_insensitive_lookup(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "hi"})
            self.assertIsNotNone(store.load_copy("b00testxyz"))

    def test_malformed_json_returns_none(self):
        with StoreTempDir() as s:
            (s.dir / "B00TESTXYZ.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(store.load_copy("B00TESTXYZ"))

    def test_non_dict_returns_none(self):
        with StoreTempDir() as s:
            (s.dir / "B00TESTXYZ.json").write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(store.load_copy("B00TESTXYZ"))


class GetHookTest(unittest.TestCase):
    def test_returns_platform_hook(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "X用コピー", "threads": "Threads用コピー"})
            self.assertEqual(store.get_hook("B00TESTXYZ", "x"), "X用コピー")
            self.assertEqual(store.get_hook("B00TESTXYZ", "threads"), "Threads用コピー")

    def test_unknown_platform_returns_none(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "hi"})
            self.assertIsNone(store.get_hook("B00TESTXYZ", "bluesky"))

    def test_missing_platform_key_falls_back(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "only x"})
            self.assertIsNone(store.get_hook("B00TESTXYZ", "threads"))

    def test_empty_or_nonstring_returns_none(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "   ", "threads": 123})
            self.assertIsNone(store.get_hook("B00TESTXYZ", "x"))
            self.assertIsNone(store.get_hook("B00TESTXYZ", "threads"))


_ARTICLE = {
    "slug": "2026-06-01-B00TESTXYZ",
    "title": "テスト商品のタイトル",
    "meta_description": "メタ説明文（fallback 用）。",
}


class NotifyBufferIntegrationTest(unittest.TestCase):
    def test_uses_store_hook_when_present(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"x": "知育◎の本命コピー"})
            text, url = notify_buffer.build_single_payload(_ARTICLE, "https://navi.omcha.jp")
            self.assertIn("知育◎の本命コピー", text)
            self.assertNotIn("メタ説明文", text)
            self.assertTrue(text.endswith(url))

    def test_falls_back_to_meta_description(self):
        with StoreTempDir():
            text, _ = notify_buffer.build_single_payload(_ARTICLE, "https://navi.omcha.jp")
            self.assertIn("メタ説明文", text)

    def test_threads_platform_hook_not_used_for_x(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"threads": "Threads専用"})
            text, _ = notify_buffer.build_single_payload(_ARTICLE, "https://navi.omcha.jp")
            self.assertNotIn("Threads専用", text)
            self.assertIn("メタ説明文", text)


class NotifyThreadsIntegrationTest(unittest.TestCase):
    def test_uses_store_hook_when_present(self):
        with StoreTempDir() as s:
            s.write("B00TESTXYZ", {"threads": "Threads向け訴求文"})
            hook, post2, url = notify_threads.build_payload(_ARTICLE, "https://navi.omcha.jp")
            self.assertEqual(hook, "Threads向け訴求文")
            self.assertEqual(post2, url)

    def test_falls_back_to_meta_description(self):
        with StoreTempDir():
            hook, _, _ = notify_threads.build_payload(_ARTICLE, "https://navi.omcha.jp")
            self.assertIn("メタ説明文", hook)


if __name__ == "__main__":
    unittest.main()
