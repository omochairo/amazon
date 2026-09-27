"""YouTube API key の quota 枯渇判定とローテーションのテスト。

日次の "Search Queries per day" 超過は 403 ではなく 429 で返る。01-fetch-products の
実行ログで 429 のまま key が切り替わらず、その query が空で終わっていた (navi-brain#76)。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_youtube  # noqa: E402

DAILY_429 = (
    '{"error": {"code": 429, "message": "Quota exceeded for quota metric '
    "'Search Queries' and limit 'Search Queries per day' of service "
    "'youtube.googleapis.com'\"}}"
)


class IsQuotaErrorTests(unittest.TestCase):
    def test_403_quota(self):
        self.assertTrue(fetch_youtube._is_quota_error(403, '{"reason": "quotaExceeded"}'))

    def test_429_daily_quota(self):
        self.assertTrue(fetch_youtube._is_quota_error(429, DAILY_429))

    def test_429_without_quota_is_not_exhaustion(self):
        self.assertFalse(fetch_youtube._is_quota_error(429, '{"reason": "rateLimitExceeded"}'))

    def test_other_status(self):
        self.assertFalse(fetch_youtube._is_quota_error(400, "quota"))


class RotateOn429Tests(unittest.TestCase):
    def setUp(self):
        self._saved = (fetch_youtube._API_KEYS, fetch_youtube._KEY_INDEX,
                       fetch_youtube._EXHAUSTED_LOGGED)
        fetch_youtube._API_KEYS = ["k1", "k2"]
        fetch_youtube._KEY_INDEX = 0
        fetch_youtube._EXHAUSTED_LOGGED = set()

    def tearDown(self):
        (fetch_youtube._API_KEYS, fetch_youtube._KEY_INDEX,
         fetch_youtube._EXHAUSTED_LOGGED) = self._saved

    def test_daily_429_rotates_to_next_key(self):
        items = [{"video_id": "v"}]
        with patch.object(fetch_youtube, "_do_search_once",
                          side_effect=[(429, DAILY_429, []), (200, "", items)]) as call:
            self.assertEqual(fetch_youtube.youtube_search("q"), items)
        self.assertEqual([c.args[0] for c in call.call_args_list], ["k1", "k2"])


if __name__ == "__main__":
    unittest.main()
