"""楽天の呼び出し同士の間隔のテスト。

楽天の上限は 1 秒に 1 回。待ちが 0.5 秒だと応答 0.4 秒前後と合わせて約 0.9 秒間隔になり、
01-fetch-products で 1 run 132 回の 429 が出ていた (navi-brain#76)。
段階検索の中で楽天を続けて呼ぶ所には、必ず RAKUTEN_REQUEST_INTERVAL の待ちが挟まることを pin する。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_cross_search  # noqa: E402


def _resp(status, payload=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload or {}
    m.text = text
    return m


class RakutenRequestIntervalTests(unittest.TestCase):
    def _run(self, responses, **kwargs):
        events = []
        it = iter(responses)

        def fake_get(url, **_):
            events.append("get")
            return next(it)

        def fake_sleep(sec):
            events.append(sec)

        with patch.object(fetch_cross_search.requests, "get", side_effect=fake_get), \
             patch.object(fetch_cross_search.time, "sleep", side_effect=fake_sleep):
            fetch_cross_search.search_rakuten_tiered(app_id="dummy", **kwargs)
        return events

    def assert_spaced(self, events, n_calls):
        self.assertEqual(events.count("get"), n_calls)
        gets = [i for i, e in enumerate(events) if e == "get"]
        for a, b in zip(gets, gets[1:]):
            waits = [e for e in events[a + 1:b] if e != "get"]
            self.assertGreaterEqual(sum(waits), fetch_cross_search.RAKUTEN_REQUEST_INTERVAL,
                                    f"calls #{a} and #{b} are not spaced: {events}")

    def test_interval_is_at_least_one_second(self):
        self.assertGreaterEqual(fetch_cross_search.RAKUTEN_REQUEST_INTERVAL, 1.0)

    def test_all_stages_spaced_when_nothing_matches(self):
        empty_books = _resp(200, {"Items": []})
        empty_ichiba = _resp(200, {"Items": []})
        # Stage0a (Books JAN) → 0b (Ichiba JAN) → 1 (Books) → 2 (Ichiba) → 3 (短縮 Ichiba)
        events = self._run([empty_books, empty_ichiba, empty_books, empty_ichiba, empty_ichiba],
                           keyword="Playmobil Ferrari GTO", jan_code="4008789711234")
        self.assert_spaced(events, 5)

    def test_stage2_trim_retry_is_spaced(self):
        empty = _resp(200, {"Items": []})
        bad = _resp(400, text='{"error_description":"keyword is not valid"}')
        # Stage1 (Books) → 2 (400) → 2 trim 再試行 → 3
        events = self._run([empty, bad, empty, empty], keyword="Playmobil Ferrari GTO 3歳")
        self.assert_spaced(events, 4)


if __name__ == "__main__":
    unittest.main()
