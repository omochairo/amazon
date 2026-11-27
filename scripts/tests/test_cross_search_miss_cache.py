"""空振りの記録 (#12305) のテスト。

マッチの無い記事の ASIN を、実行のたびに検索し直して空振りしないようにする仕組み。
守ること:
  - 見つからなかった ASIN は 14 日間、通常の実行では探し直さない
  - 一時的な失敗 (通信エラー・429・5xx) は空振りとして記録しない
  - その日の amazon.json の ASIN・--asins・JAN が変わった ASIN・新しい記事は抑制しない
  - 見つかったら記録を消す。記事の対象から外れた ASIN の記録も消す
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_cross_search as fcs  # noqa: E402

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


class MissSuppressedTest(unittest.TestCase):
    def _misses(self, days_ago, jan="4900000000001"):
        return {"B0AAAAAAAA": {"rakuten": {
            "last_searched_at": _iso(NOW - timedelta(days=days_ago)),
            "count": 1, "jan_code": jan,
        }}}

    def test_recent_miss_is_suppressed(self):
        self.assertTrue(fcs._miss_suppressed(self._misses(3), "B0AAAAAAAA", "rakuten", "4900000000001", NOW))

    def test_old_miss_is_searched_again(self):
        self.assertFalse(fcs._miss_suppressed(self._misses(14), "B0AAAAAAAA", "rakuten", "4900000000001", NOW))

    def test_jan_changed_is_searched_again(self):
        self.assertFalse(fcs._miss_suppressed(self._misses(1, jan=""), "B0AAAAAAAA", "rakuten", "4900000000001", NOW))

    def test_other_shop_or_unknown_asin_is_not_suppressed(self):
        m = self._misses(1)
        self.assertFalse(fcs._miss_suppressed(m, "B0AAAAAAAA", "yahoo", "4900000000001", NOW))
        self.assertFalse(fcs._miss_suppressed(m, "B0BBBBBBBB", "rakuten", "4900000000001", NOW))

    def test_broken_timestamp_is_not_suppressed(self):
        m = {"B0AAAAAAAA": {"rakuten": {"last_searched_at": "x", "jan_code": ""}}}
        self.assertFalse(fcs._miss_suppressed(m, "B0AAAAAAAA", "rakuten", "", NOW))


class RecordAndSaveTest(unittest.TestCase):
    def test_record_counts_up_and_clear_removes(self):
        m = {}
        fcs._record_miss(m, "B0AAAAAAAA", "rakuten", "", _iso(NOW))
        fcs._record_miss(m, "B0AAAAAAAA", "rakuten", "", _iso(NOW))
        fcs._record_miss(m, "B0AAAAAAAA", "yahoo", "", _iso(NOW))
        self.assertEqual(m["B0AAAAAAAA"]["rakuten"]["count"], 2)
        fcs._clear_miss(m, "B0AAAAAAAA", "rakuten")
        self.assertNotIn("rakuten", m["B0AAAAAAAA"])
        fcs._clear_miss(m, "B0AAAAAAAA", "yahoo")
        self.assertNotIn("B0AAAAAAAA", m)

    def test_save_drops_asins_outside_targets_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / fcs.MISS_FILE_NAME
            m = {}
            fcs._record_miss(m, "B0AAAAAAAA", "rakuten", "", _iso(NOW))
            fcs._record_miss(m, "B0GONEGONE", "rakuten", "", _iso(NOW))
            n = fcs._save_misses(path, m, keep_asins={"B0AAAAAAAA"})
            self.assertEqual(n, 1)
            loaded = fcs._load_misses(path)
            self.assertEqual(list(loaded), ["B0AAAAAAAA"])
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_load_broken_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / fcs.MISS_FILE_NAME
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(fcs._load_misses(path), {})

    def test_load_drops_malformed_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / fcs.MISS_FILE_NAME
            path.write_text(json.dumps({"items": {
                "B0AAAAAAAA": "x",
                "B0BBBBBBBB": {"rakuten": "x", "amazon": {}},
                "B0CCCCCCCC": {"yahoo": {"last_searched_at": _iso(NOW), "jan_code": ""}},
            }}), encoding="utf-8")
            loaded = fcs._load_misses(path)
            self.assertEqual(list(loaded), ["B0CCCCCCCC"])
            self.assertFalse(fcs._miss_suppressed(loaded, "B0AAAAAAAA", "rakuten", "", NOW))


class TransientFailureCountTest(unittest.TestCase):
    def _resp(self, code):
        r = MagicMock()
        r.status_code = code
        return r

    def test_counts_5xx_and_final_429_but_not_200_or_404(self):
        before = fcs._TRANSIENT_FAILURES
        with patch.object(fcs.requests, "get", side_effect=[self._resp(200), self._resp(404), self._resp(503),
                                                             self._resp(429), self._resp(429)]), \
             patch.object(fcs.time, "sleep"):
            fcs._get("t", "u")
            fcs._get("t", "u")
            self.assertEqual(fcs._TRANSIENT_FAILURES, before)
            fcs._get("t", "u")
            self.assertEqual(fcs._TRANSIENT_FAILURES, before + 1)
            fcs._get("t", "u")  # 429 → 待って再試行も 429
            self.assertEqual(fcs._TRANSIENT_FAILURES, before + 2)

    def test_auth_errors_and_broken_json_count_but_400_does_not(self):
        bad_json = self._resp(200)
        bad_json.json.side_effect = ValueError("not json")
        before = fcs._TRANSIENT_FAILURES
        with patch.object(fcs.requests, "get", side_effect=[self._resp(400), self._resp(401),
                                                             self._resp(403), bad_json]):
            fcs._get("t", "u")
            self.assertEqual(fcs._TRANSIENT_FAILURES, before)
            fcs._get("t", "u")
            fcs._get("t", "u")
            fcs._get("t", "u")
        self.assertEqual(fcs._TRANSIENT_FAILURES, before + 3)

    def test_429_then_200_is_not_a_failure(self):
        before = fcs._TRANSIENT_FAILURES
        with patch.object(fcs.requests, "get", side_effect=[self._resp(429), self._resp(200)]), \
             patch.object(fcs.time, "sleep"):
            fcs._get("t", "u")
        self.assertEqual(fcs._TRANSIENT_FAILURES, before)

    def test_network_error_counts(self):
        before = fcs._TRANSIENT_FAILURES
        with patch.object(fcs.requests, "get", side_effect=fcs.requests.exceptions.ConnectionError()):
            with self.assertRaises(fcs.requests.exceptions.ConnectionError):
                fcs._get("t", "u")
        self.assertEqual(fcs._TRANSIENT_FAILURES, before + 1)


class MainFlowTest(unittest.TestCase):
    """main() を一通り回し、検索の呼び出しと記録の中身を確かめる。"""

    TODAY = "B0TODAY001"     # その日の amazon.json にある (マッチ無し)
    ART = "B0ARTICLE1"       # 記事だけ (マッチ無し)
    HIT = "B0ARTICLE2"       # 記事だけ (検索すると見つかる)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        raw = self.root / "data" / "raw"
        (raw / "per_asin").mkdir(parents=True)
        arts = self.root / "data" / "articles"
        arts.mkdir(parents=True)
        (raw / "amazon.json").write_text(json.dumps({"items": [
            {"asin": self.TODAY, "title": "今日の商品 おもちゃ", "price": 1000},
        ]}, ensure_ascii=False), encoding="utf-8")
        for asin in (self.ART, self.HIT):
            (arts / f"{asin}.json").write_text(json.dumps(
                {"product": {"name": f"記事の商品 {asin}"}}, ensure_ascii=False), encoding="utf-8")
        self.miss_path = raw / fcs.MISS_FILE_NAME
        self._cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _run(self, argv=(), fail_asins=()):
        calls = []

        def fake_search(shop):
            def _f(keyword, *a, asin="", **k):
                calls.append((shop, asin))
                if asin in fail_asins:
                    fcs._TRANSIENT_FAILURES += 1
                    return None
                if asin == self.HIT:
                    return {"title": "見つかった", "price": 1000, "url": "https://example.com", "_match_method": "text"}
                return None
            return _f

        env = {"RAKUTEN_APP_ID": "x", "YAHOO_CLIENT_ID": "y"}
        with patch.dict(os.environ, env), \
             patch.object(fcs, "search_rakuten_tiered", side_effect=fake_search("rakuten")), \
             patch.object(fcs, "search_yahoo", side_effect=fake_search("yahoo")), \
             patch.object(fcs.time, "sleep"), \
             patch.object(sys, "argv", ["fetch_cross_search.py", *argv]):
            fcs.main()
        return calls

    def test_second_run_skips_recent_misses_but_not_todays_asin(self):
        first = self._run()
        self.assertIn(("rakuten", self.ART), first)
        misses = fcs._load_misses(self.miss_path)
        self.assertIn(self.ART, misses)
        self.assertIn(self.TODAY, misses)
        self.assertNotIn(self.HIT, misses)

        second = self._run()
        self.assertNotIn(("rakuten", self.ART), second)
        self.assertNotIn(("yahoo", self.ART), second)
        # その日の amazon.json の ASIN は記録があっても探す
        self.assertIn(("rakuten", self.TODAY), second)
        self.assertEqual(fcs._load_misses(self.miss_path)[self.ART]["rakuten"]["count"], 1)

    def test_transient_failure_is_not_recorded(self):
        self._run(fail_asins={self.ART})
        self.assertNotIn(self.ART, fcs._load_misses(self.miss_path))
        again = self._run()
        self.assertIn(("rakuten", self.ART), again)

    def test_asins_mode_searches_despite_miss_and_keeps_other_records(self):
        self._run()
        calls = self._run(argv=["--asins", self.ART])
        self.assertIn(("rakuten", self.ART), calls)
        misses = fcs._load_misses(self.miss_path)
        self.assertEqual(misses[self.ART]["rakuten"]["count"], 2)
        self.assertIn(self.TODAY, misses)  # --asins は対象外の記録を消さない

    def test_jan_added_later_is_searched_again(self):
        self._run()
        snap = self.root / "data" / "raw" / "per_asin" / self.ART
        snap.mkdir(parents=True)
        (snap / "amazon.json").write_text(json.dumps({"item": {"jan_code": "4900000000001"}}), encoding="utf-8")
        calls = self._run()
        self.assertIn(("rakuten", self.ART), calls)
        self.assertEqual(fcs._load_misses(self.miss_path)[self.ART]["rakuten"]["jan_code"], "4900000000001")

    def test_low_confidence_mode_searches_despite_miss(self):
        self._run()
        with patch("build_post._matched_passes_quality", return_value=True):
            calls = self._run(argv=["--re-search-low-confidence"])
        self.assertIn(("rakuten", self.ART), calls)

    def test_gone_match_is_searched_again_then_held(self):
        gone = {"matched_asin": self.ART, "title": "古い", "price": 1, "url": "u",
                "_gone_at": "2026-11-20T00:00:00+00:00"}
        live = {"matched_asin": self.ART, "title": "生きている", "price": 1, "url": "u"}
        raw = self.root / "data" / "raw"
        (raw / "rakuten_matched.json").write_text(json.dumps({"items": [gone]}), encoding="utf-8")
        (raw / "yahoo_matched.json").write_text(json.dumps({"items": [live]}), encoding="utf-8")
        first = self._run()
        self.assertIn(("rakuten", self.ART), first)
        self.assertNotIn(("yahoo", self.ART), first)  # 生きているマッチは探さない
        self.assertIn("rakuten", fcs._load_misses(self.miss_path)[self.ART])
        second = self._run()
        self.assertNotIn(("rakuten", self.ART), second)  # 空振りの記録で 14 日は止まる
        kept = json.loads((raw / "rakuten_matched.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(kept[0]["_gone_at"], gone["_gone_at"])  # 見つからなくても消さない

    def test_record_removed_when_article_disappears(self):
        self._run()
        (self.root / "data" / "articles" / f"{self.ART}.json").unlink()
        self._run()
        self.assertNotIn(self.ART, fcs._load_misses(self.miss_path))


if __name__ == "__main__":
    unittest.main()
