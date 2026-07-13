"""Unit tests for fetch_price_watch (#3046 / #2995 案8 P1)。

カバレッジ:
1. collect_published_asins: ASIN 抽出・sidecar 除外・大文字化重複排除・ソート
2. extract_price / extract_availability: 正常系・欠落フィールド
3. run(): 価格変化点の history 追記・latest.json のスキーマ・欠落 ASIN の
   カウント・バッチ例外時の continue・全バッチ失敗時の exit code 1・
   dry-run が API 呼び出し/書き込みをしないこと
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_price_watch as F  # noqa: E402


def _write_article(dir_path: pathlib.Path, filename: str, asin: str | None) -> pathlib.Path:
    p = dir_path / filename
    product = {"asin": asin} if asin is not None else {}
    p.write_text(json.dumps({"product": product}, ensure_ascii=False), encoding="utf-8")
    return p


def _item(asin: str, price: int | None, availability: str | None) -> dict:
    listings = []
    if price is not None or availability is not None:
        listing: dict = {}
        if price is not None:
            listing["price"] = {"money": {"amount": price}}
        if availability is not None:
            listing["availability"] = {"message": availability}
        listings.append(listing)
    return {"asin": asin, "offersV2": {"listings": listings}}


def _response(items: list[dict]) -> dict:
    return {"itemsResult": {"items": items}}


class CollectPublishedAsinsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.articles_dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_and_uppercases_and_dedupes(self):
        _write_article(self.articles_dir, "2026-05-01-b0xxxxxxxx.json", "b0xxxxxxxx")
        _write_article(self.articles_dir, "2026-06-01-other.json", "B0XXXXXXXX")  # dup, different case already
        out = F.collect_published_asins(self.articles_dir)
        self.assertEqual(out, ["B0XXXXXXXX"])

    def test_excludes_sidecars(self):
        _write_article(self.articles_dir, "2026-05-01-B0AAAAAAAA.json", "B0AAAAAAAA")
        (self.articles_dir / "2026-05-01-B0AAAAAAAA.quality.json").write_text(
            json.dumps({"product": {"asin": "B0BBBBBBBB"}}), encoding="utf-8")
        (self.articles_dir / "2026-05-01-B0AAAAAAAA.enrichment.json").write_text(
            json.dumps({"product": {"asin": "B0CCCCCCCC"}}), encoding="utf-8")
        (self.articles_dir / "2026-05-01-B0AAAAAAAA.seo.json").write_text(
            json.dumps({"product": {"asin": "B0DDDDDDDD"}}), encoding="utf-8")
        out = F.collect_published_asins(self.articles_dir)
        self.assertEqual(out, ["B0AAAAAAAA"])

    def test_sorted_output(self):
        _write_article(self.articles_dir, "a.json", "B0ZZZZZZZZ")
        _write_article(self.articles_dir, "b.json", "B0AAAAAAAA")
        out = F.collect_published_asins(self.articles_dir)
        self.assertEqual(out, ["B0AAAAAAAA", "B0ZZZZZZZZ"])

    def test_missing_or_empty_asin_skipped(self):
        _write_article(self.articles_dir, "a.json", None)
        _write_article(self.articles_dir, "b.json", "")
        out = F.collect_published_asins(self.articles_dir)
        self.assertEqual(out, [])

    def test_missing_dir_returns_empty(self):
        out = F.collect_published_asins(self.articles_dir / "does-not-exist")
        self.assertEqual(out, [])

    def test_malformed_json_skipped(self):
        (self.articles_dir / "broken.json").write_text("not json", encoding="utf-8")
        out = F.collect_published_asins(self.articles_dir)
        self.assertEqual(out, [])


class ExtractFieldsTest(unittest.TestCase):
    def test_extract_price_normal(self):
        it = _item("B1", 1980, "在庫あり")
        self.assertEqual(F.extract_price(it), 1980)

    def test_extract_price_missing(self):
        it = {"asin": "B1", "offersV2": {"listings": []}}
        self.assertIsNone(F.extract_price(it))

    def test_extract_availability_normal(self):
        it = _item("B1", 1980, "残り2点")
        self.assertEqual(F.extract_availability(it), "残り2点")

    def test_extract_availability_missing(self):
        it = {"asin": "B1", "offersV2": {"listings": []}}
        self.assertIsNone(F.extract_availability(it))


class FakeApi:
    """CreatorsAPIClient の代わりに get_items だけを差し替えるテスト用スタブ。"""

    def __init__(self, responses=None, exceptions=None, bad_shapes=None):
        # 呼び出し順に対応する dict: {tuple(chunk): response|Exception}
        self.responses = responses or {}
        self.exceptions = exceptions or set()
        self.bad_shapes = bad_shapes or set()
        self.calls: list[list[str]] = []

    def get_items(self, asins, resources=None):
        self.calls.append(list(asins))
        key = tuple(asins)
        if key in self.exceptions:
            raise RuntimeError("simulated API failure")
        if key in self.bad_shapes:
            return {"itemsResult": {}}  # items missing -> unexpected shape
        return self.responses.get(key, _response([]))


class RunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.articles_dir = self.root / "articles"
        self.out_dir = self.root / "price_watch"
        self.articles_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_history_appended_and_latest_schema(self):
        _write_article(self.articles_dir, "a.json", "B0AAAAAAAA")
        _write_article(self.articles_dir, "b.json", "B0BBBBBBBB")
        chunk = ("B0AAAAAAAA", "B0BBBBBBBB")
        api = FakeApi(responses={
            chunk: _response([_item("B0AAAAAAAA", 1980, "在庫あり")]),
        })
        result = F.run(api, self.articles_dir, self.out_dir, sleeper=lambda s: None)

        self.assertEqual(result["source"], "creators-api")
        self.assertIn("generated_at", result)
        self.assertEqual(result["stats"], {
            "requested": 2, "found": 1, "missing": 1, "history_appended": 1,
        })
        self.assertEqual(set(result["items"].keys()), {"B0AAAAAAAA"})
        self.assertEqual(result["items"]["B0AAAAAAAA"]["p"], 1980)
        self.assertEqual(result["items"]["B0AAAAAAAA"]["avail"], "在庫あり")
        self.assertIn("ts", result["items"]["B0AAAAAAAA"])

        latest_path = self.out_dir / "latest.json"
        self.assertTrue(latest_path.exists())
        on_disk = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["stats"]["found"], 1)

        history_path = self.out_dir / "history" / "B0AAAAAAAA.jsonl"
        self.assertTrue(history_path.exists())
        rec = json.loads(history_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["price"], 1980)
        self.assertEqual(rec["source"], "amazon")

    def test_missing_asin_counted_only_no_history(self):
        _write_article(self.articles_dir, "a.json", "B0AAAAAAAA")
        api = FakeApi(responses={("B0AAAAAAAA",): _response([])})
        result = F.run(api, self.articles_dir, self.out_dir, sleeper=lambda s: None)
        self.assertEqual(result["stats"]["missing"], 1)
        self.assertEqual(result["stats"]["found"], 0)
        self.assertEqual(result["items"], {})
        self.assertFalse((self.out_dir / "history").exists())

    def test_batch_exception_continues_to_next_batch(self):
        asins = [f"B0{i:08d}"[:10].upper() for i in range(11)]  # 11 asins -> 2 batches
        for idx, asin in enumerate(asins):
            _write_article(self.articles_dir, f"{idx}.json", asin)
        collected = F.collect_published_asins(self.articles_dir)
        self.assertEqual(len(collected), 11)
        chunk1 = tuple(collected[:10])
        chunk2 = tuple(collected[10:])
        api = FakeApi(
            exceptions={chunk1},
            responses={chunk2: _response([_item(collected[10], 500, "在庫あり")])},
        )
        result = F.run(api, self.articles_dir, self.out_dir, sleeper=lambda s: None)
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(result["stats"]["found"], 1)
        self.assertEqual(result["stats"]["missing"], 10)

    def test_unexpected_response_shape_treated_as_batch_failure(self):
        # 2 バッチ用意し、1つだけ想定外形状にする (全滅にすると exit 1 経路に
        # 入ってしまい別テストと重複するため、continue 側の検証はここで行う)。
        asins = [f"B0{i:08d}" for i in range(11)]
        for idx, asin in enumerate(asins):
            _write_article(self.articles_dir, f"{idx}.json", asin)
        collected = F.collect_published_asins(self.articles_dir)
        chunk1 = tuple(collected[:10])
        chunk2 = tuple(collected[10:])
        api = FakeApi(
            bad_shapes={chunk1},
            responses={chunk2: _response([_item(collected[10], 500, "在庫あり")])},
        )
        result = F.run(api, self.articles_dir, self.out_dir, sleeper=lambda s: None)
        self.assertEqual(result["stats"]["found"], 1)
        self.assertEqual(result["stats"]["missing"], 10)

    def test_all_batches_failed_raises_system_exit_1(self):
        _write_article(self.articles_dir, "a.json", "B0AAAAAAAA")
        api = FakeApi(exceptions={("B0AAAAAAAA",)})
        with self.assertRaises(SystemExit) as ctx:
            F.run(api, self.articles_dir, self.out_dir, sleeper=lambda s: None)
        self.assertEqual(ctx.exception.code, 1)
        # latest.json は失敗時も書かれる (呼び出し元が観測できるように)
        self.assertTrue((self.out_dir / "latest.json").exists())

    def test_no_asins_does_not_raise(self):
        result = F.run(FakeApi(), self.articles_dir, self.out_dir, sleeper=lambda s: None)
        self.assertEqual(result["stats"]["requested"], 0)

    def test_max_asins_caps_targets(self):
        _write_article(self.articles_dir, "a.json", "B0AAAAAAAA")
        _write_article(self.articles_dir, "b.json", "B0BBBBBBBB")
        api = FakeApi(responses={("B0AAAAAAAA",): _response([_item("B0AAAAAAAA", 100, None)])})
        result = F.run(api, self.articles_dir, self.out_dir, max_asins=1, sleeper=lambda s: None)
        self.assertEqual(result["stats"]["requested"], 1)
        self.assertEqual(api.calls, [["B0AAAAAAAA"]])

    def test_dry_run_makes_no_api_calls_or_writes(self):
        _write_article(self.articles_dir, "a.json", "B0AAAAAAAA")
        api = mock.Mock()
        result = F.run(api, self.articles_dir, self.out_dir, dry_run=True, sleeper=lambda s: None)
        api.get_items.assert_not_called()
        self.assertEqual(result["stats"]["requested"], 1)
        self.assertFalse(self.out_dir.exists())


if __name__ == "__main__":
    unittest.main()
