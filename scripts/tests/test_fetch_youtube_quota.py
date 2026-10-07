"""YouTube API key の quota 枯渇判定とローテーションのテスト。

日次の "Search Queries per day" 超過は 403 ではなく 429 で返る。01-fetch-products の
実行ログで 429 のまま key が切り替わらず、その query が空で終わっていた (navi-brain#76)。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _fetch_targets  # noqa: E402
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
        # _do_search_once が実際に返す形 (title / url / thumbnail)
        items = [{"title": "t", "url": "https://www.youtube.com/watch?v=vvvvvvvvvvv",
                  "thumbnail": ""}]
        with patch.object(fetch_youtube, "_do_search_once",
                          side_effect=[(429, DAILY_429, []), (200, "", items)]) as call:
            self.assertEqual(fetch_youtube.youtube_search("q"), items)
        self.assertEqual([c.args[0] for c in call.call_args_list], ["k1", "k2"])

    def test_zero_hits_and_failure_are_distinguished(self):
        # 0 件は []、取得失敗は None。失敗を [] にすると per-ASIN raw が空で
        # 上書きされ、前回取れていた動画が消える。
        with patch.object(fetch_youtube, "_do_search_once", return_value=(200, "", [])):
            self.assertEqual(fetch_youtube.youtube_search("q"), [])
        with patch.object(fetch_youtube, "_do_search_once", return_value=(500, "boom", [])):
            self.assertIsNone(fetch_youtube.youtube_search("q"))
        with patch.object(fetch_youtube, "_do_search_once",
                          side_effect=[(429, DAILY_429, []), (429, DAILY_429, [])]):
            self.assertIsNone(fetch_youtube.youtube_search("q"))
        # 全 key 枯渇後は呼び出さずに None
        with patch.object(fetch_youtube, "_do_search_once") as call:
            self.assertIsNone(fetch_youtube.youtube_search("q"))
        call.assert_not_called()


class MainKeepsPriorRawOnFailureTests(unittest.TestCase):
    """取得失敗した ASIN の per-ASIN raw を空で上書きせず、state も更新しない。"""

    def setUp(self):
        import tempfile
        # main() が書き換えるモジュールのグローバルを tearDown で戻す
        self._saved = (fetch_youtube._API_KEYS, fetch_youtube._KEY_INDEX,
                       fetch_youtube._EXHAUSTED_LOGGED)
        self._tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self._tmp.name) / "raw"
        self.out.mkdir()
        (self.out / "amazon.json").write_text(json.dumps({"items": [
            {"asin": "B0OK000001", "title": "レゴ 71439"},
            {"asin": "B0FAIL0001", "title": "レゴ 60337"},
        ]}), encoding="utf-8")
        self.prior = [{"title": "前回の動画", "url": "https://www.youtube.com/watch?v=prior000001",
                       "thumbnail": ""}]
        _fetch_targets.write_per_asin_raw(self.out, "youtube", "B0FAIL0001", "old", self.prior)

    def tearDown(self):
        (fetch_youtube._API_KEYS, fetch_youtube._KEY_INDEX,
         fetch_youtube._EXHAUSTED_LOGGED) = self._saved
        self._tmp.cleanup()

    def test_failed_asin_keeps_raw_and_stays_stale(self):
        hit = [{"title": "新しい動画", "url": "https://www.youtube.com/watch?v=new00000001",
                "thumbnail": ""}]

        def fake_search(query, max_results=5):
            if "60337" in query:
                return None      # この ASIN だけ取得失敗
            if "71439" in query:
                return hit
            return []            # ジャンル検索は 0 件

        argv = ["fetch_youtube.py", "--out", str(self.out),
                "--articles-dir", str(self.out / "no_articles")]
        with patch.object(fetch_youtube, "load_api_keys", return_value=["k1"]), \
             patch.object(fetch_youtube, "youtube_search", side_effect=fake_search), \
             patch.object(sys, "argv", argv):
            fetch_youtube.main()

        self.assertEqual(
            _fetch_targets.load_per_asin_raw_items(self.out, "youtube", "B0FAIL0001"),
            self.prior)
        self.assertEqual(
            _fetch_targets.load_per_asin_raw_items(self.out, "youtube", "B0OK000001"), hit)
        state = _fetch_targets.load_state(self.out)["youtube"]
        self.assertIn("B0OK000001", state)
        self.assertNotIn("B0FAIL0001", state)


class KnownBrandsInSyncTests(unittest.TestCase):
    def test_fetch_brands_cover_filter_brands(self):
        # 「filter_raw_per_asin.py と一致させること」が守られず 3 ブランド欠けていた。
        # fetch 側に無いブランドは検索語がタイトル先頭 30 字に落ちる。
        import filter_raw_per_asin
        missing = set(filter_raw_per_asin.KNOWN_BRANDS) - set(fetch_youtube.KNOWN_BRANDS)
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
