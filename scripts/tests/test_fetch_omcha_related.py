"""fetch_omcha_related.py のユニットテスト (Issue #674)。

カバレッジ:
1. `_keyword_from_tags` が build_post の旧 `_omcha_keyword_from_tags` と完全同一
2. `_collect_keyword_pairs` が記事 JSON の tags から keyword 抽出
3. `_pick_stale_targets` が state を見て stale-first で picking
4. main flow: stale ASIN のみ get_related_articles を呼び、結果が per_asin に書かれる
5. build_post._attach_omcha_related が tracked キャッシュを読んで UTM 装飾するだけ
   (live HTTP しないことの回帰防止)
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_post  # noqa: E402
import fetch_omcha_related  # noqa: E402
import internal_links  # noqa: E402


class KeywordFromTagsTest(unittest.TestCase):
    def test_top3_tags_joined(self):
        self.assertEqual(
            fetch_omcha_related._keyword_from_tags(["知育玩具", "パズル", "3歳", "余分"]),
            "知育玩具 パズル 3歳",
        )

    def test_handles_empty_or_invalid(self):
        self.assertEqual(fetch_omcha_related._keyword_from_tags(None), "")
        self.assertEqual(fetch_omcha_related._keyword_from_tags([]), "")
        self.assertEqual(fetch_omcha_related._keyword_from_tags(["", "   "]), "")
        self.assertEqual(fetch_omcha_related._keyword_from_tags("notalist"), "")

    def test_skips_non_string_entries(self):
        self.assertEqual(
            fetch_omcha_related._keyword_from_tags(["A", 123, None, "B", "C"]),
            "A B C",
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            fetch_omcha_related._keyword_from_tags(["  A  ", " B"]),
            "A B",
        )


class CollectKeywordPairsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.articles = pathlib.Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_article(self, name: str, payload: dict) -> None:
        (self.articles / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_collects_from_articles(self):
        self._write_article("2026-05-24-B000TEST01.json", {"tags": ["木製", "知育", "3歳"]})
        self._write_article("2026-05-24-B000TEST02.json", {"tags": ["ブロック", "5歳"]})
        out = fetch_omcha_related._collect_keyword_pairs(self.articles)
        self.assertEqual(out, {
            "B000TEST01": "木製 知育 3歳",
            "B000TEST02": "ブロック 5歳",
        })

    def test_skips_sidecar_and_no_tags(self):
        self._write_article("2026-05-24-B000TEST03.json", {"tags": []})  # no usable tags
        self._write_article("2026-05-24-B000TEST04.quality.json", {"tags": ["X"]})  # sidecar
        self._write_article("2026-05-24-B000TEST05.enrichment.json", {"tags": ["X"]})  # sidecar
        out = fetch_omcha_related._collect_keyword_pairs(self.articles)
        self.assertEqual(out, {})

    def test_returns_empty_when_dir_missing(self):
        out = fetch_omcha_related._collect_keyword_pairs(pathlib.Path("/__nope__"))
        self.assertEqual(out, {})


class PickStaleTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_state(self, state: dict) -> None:
        (self.out_dir / "_fetch_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8",
        )

    def test_picks_unseen_asins_first(self):
        now = datetime(2026, 5, 24, tzinfo=timezone.utc)
        self._write_state({"omcha": {
            "B000FRESH1": (now - timedelta(days=1)).isoformat(),  # fresh
            "B000STALE1": (now - timedelta(days=10)).isoformat(),  # stale
        }})
        keyword_pairs = {
            "B000FRESH1": "kw1", "B000STALE1": "kw2", "B000NEW01": "kw3",
        }
        picked = fetch_omcha_related._pick_stale_targets(
            self.out_dir, keyword_pairs, max_per_run=10, stale_after_days=7, now=now,
        )
        picked_asins = [a for a, _ in picked]
        self.assertIn("B000NEW01", picked_asins)
        self.assertIn("B000STALE1", picked_asins)
        self.assertNotIn("B000FRESH1", picked_asins)

    def test_respects_max_per_run_cap(self):
        now = datetime(2026, 5, 24, tzinfo=timezone.utc)
        keyword_pairs = {f"B{i:09d}": f"kw{i}" for i in range(100)}
        picked = fetch_omcha_related._pick_stale_targets(
            self.out_dir, keyword_pairs, max_per_run=10, stale_after_days=7, now=now,
        )
        self.assertEqual(len(picked), 10)


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmpdir.name)
        self.out_dir = self.root / "data" / "raw"
        self.articles = self.root / "data" / "articles"
        self.articles.mkdir(parents=True)
        (self.articles / "2026-05-24-B000NEW001.json").write_text(
            json.dumps({"tags": ["木製", "知育"]}, ensure_ascii=False), encoding="utf-8",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_main_writes_per_asin_cache_and_marks_state(self):
        fake_items = [{"title": "テスト関連記事", "url": "https://omcha.jp/p1",
                       "score": 80, "thumbnail": "https://omcha.jp/t.jpg"}]
        argv = [
            "fetch_omcha_related.py",
            "--out", str(self.out_dir),
            "--articles-dir", str(self.articles),
            "--sleep", "0",
        ]
        with patch.object(sys, "argv", argv), \
             patch.object(fetch_omcha_related, "get_related_articles", return_value=fake_items) as mock_get:
            fetch_omcha_related.main()
        # min_score は iro/v2 の 0..100 スケール既定値 (Issue #6103)。
        mock_get.assert_called_once_with(
            "木製 知育", count=3, min_score=internal_links.DEFAULT_MIN_SCORE
        )
        # cache 書き込み確認
        cache = self.out_dir / "per_asin" / "B000NEW001" / "omcha_related.json"
        self.assertTrue(cache.exists())
        data = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(data["keyword"], "木製 知育")
        self.assertEqual(data["items"], fake_items)
        # state 更新確認
        state = json.loads((self.out_dir / "_fetch_state.json").read_text(encoding="utf-8"))
        self.assertIn("B000NEW001", state.get("omcha", {}))


class BuildPostAttachOmchaRelatedTest(unittest.TestCase):
    """build_post._attach_omcha_related が live HTTP しない (read-only) ことの回帰防止。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.per_asin_root = pathlib.Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_cache(self, asin: str, items: list) -> None:
        d = self.per_asin_root / asin
        d.mkdir(parents=True, exist_ok=True)
        (d / "omcha_related.json").write_text(
            json.dumps({"keyword": "kw", "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_reads_cache_and_decorates_url_with_utm(self):
        self._write_cache("B000ABCDEF", [
            {"title": "x", "url": "https://omcha.jp/post1", "score": 50,
             "thumbnail": "https://omcha.jp/t1.jpg"},
        ])
        data = {"product": {"asin": "B000ABCDEF"}}
        build_post._attach_omcha_related(data, self.per_asin_root)
        self.assertIn("omcha_related", data)
        self.assertEqual(len(data["omcha_related"]), 1)
        decorated_url = data["omcha_related"][0]["url"]
        self.assertIn("utm_source=omochairo-amazon", decorated_url)
        self.assertIn("utm_content=B000ABCDEF", decorated_url)

    def test_noop_when_cache_missing(self):
        data = {"product": {"asin": "B000MISSING"}}
        build_post._attach_omcha_related(data, self.per_asin_root)
        self.assertNotIn("omcha_related", data)

    def test_noop_when_no_asin(self):
        data = {"product": {}}
        build_post._attach_omcha_related(data, self.per_asin_root)
        self.assertNotIn("omcha_related", data)

    def test_skips_old_schema_without_thumbnail(self):
        # thumbnail フィールドの無い古いキャッシュは無視
        self._write_cache("B000OLDFMT0", [
            {"title": "x", "url": "https://omcha.jp/x", "score": 50},
        ])
        data = {"product": {"asin": "B000OLDFMT0"}}
        build_post._attach_omcha_related(data, self.per_asin_root)
        self.assertNotIn("omcha_related", data)

    def test_preserves_jules_authored_field(self):
        # 将来 Jules が omcha_related を直接書く場合の予約: 既存値は上書きしない
        pre = [{"title": "from-jules", "url": "https://omcha.jp/j",
                "score": 100, "thumbnail": "https://omcha.jp/j.jpg"}]
        data = {"product": {"asin": "B000ABCDEF"}, "omcha_related": pre}
        build_post._attach_omcha_related(data, self.per_asin_root)
        self.assertEqual(data["omcha_related"], pre)


if __name__ == "__main__":
    unittest.main()
