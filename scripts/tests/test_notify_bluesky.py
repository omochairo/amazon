"""Unit tests for notify_bluesky (#1598).

ネットワークを使わない純ロジック (本文/カードの組み立て・truncate・record schema・
thumb 省略時のフォールバック・画像サイズガード) を検証。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import notify_bluesky as B  # noqa: E402


def _article(**over) -> dict:
    base = {
        "slug": "lego-classic-b0dbtlh8zm",
        "title": "レゴ クラシック 黄色のアイデアボックス",
        "meta_description": "知育に人気のレゴ基礎セットを 3 サイト最安値で比較。",
        "product": {"image": "https://m.media-amazon.com/images/I/abc._SL500_.jpg"},
    }
    base.update(over)
    return base


class TruncateTest(unittest.TestCase):
    def test_under_limit_unchanged(self):
        self.assertEqual(B._truncate("あいう", 10), "あいう")

    def test_over_limit_ellipsis(self):
        out = B._truncate("あ" * 50, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_none_safe(self):
        self.assertEqual(B._truncate(None, 10), "")


class BuildPayloadTest(unittest.TestCase):
    def test_url_uses_lowercase_asin(self):
        p = B.build_payload(_article(), "https://navi.omcha.jp")
        self.assertEqual(p["url"], "https://navi.omcha.jp/products/b0dbtlh8zm/")
        self.assertEqual(p["og_image_url"], "https://navi.omcha.jp/og/b0dbtlh8zm.jpg")

    def test_text_prefers_store_then_meta(self):
        # store コピー無し → meta_description fallback
        with mock.patch.object(B.sns_copy_store, "get_hook", return_value=None):
            p = B.build_payload(_article(), "https://navi.omcha.jp")
            self.assertEqual(p["text"], "知育に人気のレゴ基礎セットを 3 サイト最安値で比較。")
        # store コピー有り → 優先
        with mock.patch.object(B.sns_copy_store, "get_hook", return_value="ストア訴求文"):
            p = B.build_payload(_article(), "https://navi.omcha.jp")
            self.assertEqual(p["text"], "ストア訴求文")

    def test_text_truncated_to_limit(self):
        with mock.patch.object(B.sns_copy_store, "get_hook", return_value="あ" * 400):
            p = B.build_payload(_article(), "https://navi.omcha.jp")
            self.assertLessEqual(len(p["text"]), B.BLUESKY_TEXT_LIMIT)

    def test_fallback_image_from_product(self):
        p = B.build_payload(_article(), "https://navi.omcha.jp")
        self.assertTrue(p["fallback_image"].startswith("https://m.media-amazon.com/"))


class BuildRecordTest(unittest.TestCase):
    def _payload(self):
        with mock.patch.object(B.sns_copy_store, "get_hook", return_value="hook"):
            return B.build_payload(_article(), "https://navi.omcha.jp")

    def test_record_schema_with_thumb(self):
        rec = B.build_record(self._payload(), {"$type": "blob", "ref": {"$link": "x"}})
        self.assertEqual(rec["$type"], "app.bsky.feed.post")
        self.assertEqual(rec["langs"], ["ja"])
        self.assertTrue(rec["createdAt"].endswith("Z"))
        ext = rec["embed"]["external"]
        self.assertEqual(rec["embed"]["$type"], "app.bsky.embed.external")
        self.assertIn("thumb", ext)
        self.assertEqual(ext["uri"], "https://navi.omcha.jp/products/b0dbtlh8zm/")

    def test_record_without_thumb_omits_key(self):
        rec = B.build_record(self._payload(), None)
        self.assertNotIn("thumb", rec["embed"]["external"])


class FetchImageGuardTest(unittest.TestCase):
    def test_skips_when_all_blank(self):
        self.assertIsNone(B._fetch_image("", None))

    def test_oversize_blob_rejected(self):
        big = b"x" * (B.BLOB_MAX_BYTES + 1)

        class FakeResp:
            headers = {"Content-Type": "image/jpeg"}

            def read(self):
                return big

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(B.urllib.request, "urlopen", return_value=FakeResp()):
            self.assertIsNone(B._fetch_image("https://navi.omcha.jp/og/x.jpg"))


if __name__ == "__main__":
    unittest.main()
