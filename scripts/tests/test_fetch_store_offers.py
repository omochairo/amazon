"""fetch_store_offers.py と where_to_buy_format.build_store_rows のテスト。

守ること:
  - 楽天は shopCode・JAN・availability=0、Yahoo は seller_id・EAN-13 の jan_code で引く
  - その店の出品 (itemCode の店コード / URL のストア ID) で、中古でないものだけ採る
  - 一時的な失敗・飛ばしたものは前の記録を変えない。見つからなければ missing を記録する
  - 同じ店の候補が複数なら、在庫あり・価格ありを優先する
  - 記録が無い・JAN が変わったものが先、あとは古い順。最近確かめたものは除く
  - 書き込みのとき、記事の対象から外れた ASIN の記録は消し、ほかは残す
  - 同じ日の 2 回目は何もしない。dry-run と 1 件も確かめられなかった回は何も書かない
  - 在庫表の行は「見つかった・14 日以内・同じ店の行がまだ無い」ものだけ
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

import fetch_store_offers as fso  # noqa: E402
import refresh_shop_prices as rsp  # noqa: E402
import where_to_buy_format as wtb  # noqa: E402

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
KEYS = {"rakuten_app_id": "a", "rakuten_access_key": "k", "rakuten_aff_id": "aff",
        "yahoo_client_id": "y", "vc_sid": "1", "vc_pid": "2"}
TR_URL = "https://hb.afl.rakuten.co.jp/hgc/x/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Ftoysrus%2F123%2F"
TY_URL = ("https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1&pid=2&vc_url="
          "https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Ftoysrus-babierus%2F456.html")


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def _ritem(code="toysrus:123", name="Xライズスラッシャー", price=7480, availability=1):
    return {"itemCode": code, "itemName": name, "itemPrice": price, "availability": availability,
            "affiliateUrl": TR_URL}


class CheckRakutenTest(unittest.TestCase):
    def _call(self, resp):
        with patch.object(rsp.requests, "get", return_value=resp) as g, \
             patch.object(rsp.time, "sleep"):
            out = fso.check("toysrus_rakuten", "4904810123456", KEYS)
        return out, g

    def test_requests_shop_code_jan_and_availability_0(self):
        (outcome, item), g = self._call(_resp(200, {"Items": [_ritem()]}))
        self.assertEqual(outcome, fso.FOUND)
        self.assertEqual(item["price"], 7480)
        params = g.call_args.kwargs["params"]
        self.assertEqual(params["shopCode"], "toysrus")
        self.assertEqual(params["keyword"], "4904810123456")
        self.assertEqual(params["availability"], 0)
        self.assertEqual(params["affiliateId"], "aff")

    def test_other_shop_and_used_items_are_missing(self):
        body = {"Items": [_ritem(code="other:1"), _ritem(name="【中古】Xライズスラッシャー")]}
        (outcome, _), _g = self._call(_resp(200, body))
        self.assertEqual(outcome, fso.MISSING)

    def test_prefers_in_stock_with_price(self):
        body = {"Items": [_ritem(code="toysrus:old", availability=0),
                          _ritem(code="toysrus:zero", price=0),
                          _ritem(code="toysrus:new")]}
        (outcome, item), _g = self._call(_resp(200, body))
        self.assertEqual((outcome, item["itemCode"]), (fso.FOUND, "toysrus:new"))

    def test_404_and_empty_are_missing(self):
        self.assertEqual(self._call(_resp(404, {}))[0][0], fso.MISSING)
        self.assertEqual(self._call(_resp(200, {"Items": []}))[0][0], fso.MISSING)

    def test_failures(self):
        self.assertEqual(self._call(_resp(503, {}))[0][0], fso.FAILED)
        self.assertEqual(self._call(_resp(400, {}))[0][0], fso.SKIPPED)


class CheckYahooTest(unittest.TestCase):
    def _call(self, body, jan="490481012345"):
        with patch.object(rsp.requests, "get", return_value=_resp(200, body)) as g, \
             patch.object(rsp.time, "sleep"):
            out = fso.check("toysrus_yahoo", jan, KEYS)
        return out, g

    def test_requests_seller_and_ean13_and_wraps_vc(self):
        raw = "https://store.shopping.yahoo.co.jp/toysrus-babierus/456.html"
        (outcome, item), g = self._call({"hits": [{"url": raw, "name": "X", "price": 7480,
                                                   "inStock": True}]})
        self.assertEqual(outcome, fso.FOUND)
        self.assertIn("valuecommerce.com", item["url"])
        params = g.call_args.kwargs["params"]
        self.assertEqual(params["seller_id"], "toysrus-babierus")
        self.assertEqual(len(params["jan_code"]), 13)

    def test_other_seller_is_missing(self):
        raw = "https://store.shopping.yahoo.co.jp/tenbai/1.html"
        (outcome, _), _g = self._call({"hits": [{"url": raw, "name": "X", "price": 17480}]})
        self.assertEqual(outcome, fso.MISSING)

    def test_out_of_stock(self):
        raw = "https://store.shopping.yahoo.co.jp/toysrus-babierus/456.html"
        (_o, item), _g = self._call({"hits": [{"url": raw, "name": "X", "price": 7480,
                                               "inStock": False}]})
        self.assertEqual(item["availability"], "outofstock")


class ApplyTest(unittest.TestCase):
    def test_found_missing_and_failures(self):
        iso = NOW.isoformat()
        rec = fso.apply_result(None, fso.FOUND, {"price": 7480, "url": TR_URL, "title": "X"}, "j", iso)
        self.assertEqual((rec["status"], rec["price"], rec["checked_at"]), ("found", 7480, iso))
        self.assertEqual(fso.apply_result(rec, fso.MISSING, None, "j", iso)["status"], "missing")
        self.assertIs(fso.apply_result(rec, fso.FAILED, None, "j", iso), rec)
        self.assertIs(fso.apply_result(rec, fso.SKIPPED, None, "j", iso), rec)
        # 金額の無い「見つかった」は記録を変えない
        self.assertIs(fso.apply_result(rec, fso.FOUND, {"price": 0, "url": TR_URL}, "j", iso), rec)


class PickTest(unittest.TestCase):
    def test_new_and_jan_changed_first_then_oldest_skip_recent_and_matched(self):
        # targets の並びは記事の新しい順 (collect_targets が並べる)
        targets = {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"}
        old = (NOW - timedelta(days=30)).isoformat()
        older = (NOW - timedelta(days=40)).isoformat()
        recent = (NOW - timedelta(days=2)).isoformat()
        records = {
            "A": {"toysrus_rakuten": {"jan": "1", "checked_at": old}},
            "B": {"toysrus_rakuten": {"jan": "2", "checked_at": older}},
            "C": {"toysrus_rakuten": {"jan": "3", "checked_at": recent}},
            "D": {"toysrus_rakuten": {"jan": "old-jan", "checked_at": recent}},
        }
        got = fso.pick(records, "toysrus_rakuten", targets, NOW, 10, fso.MIN_AGE_DAYS)
        self.assertEqual(got, ["D", "E", "B", "A"])
        self.assertEqual(fso.pick(records, "toysrus_rakuten", targets, NOW, 2,
                                  fso.MIN_AGE_DAYS), ["D", "E"])

    def test_collect_targets_needs_rollout_date_and_jan(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            arts, per = root / "articles", root / "per_asin"
            arts.mkdir()
            for asin, date, jan in (("B0AAAAAAAA", "2026-11-01", "4900000000001"),
                                    ("B0BBBBBBBB", "2026-07-01", "4900000000002"),
                                    ("B0CCCCCCCC", "2026-11-01", "")):
                (arts / f"x-{asin}.json").write_text(json.dumps({"date": date}), encoding="utf-8")
                (per / asin).mkdir(parents=True)
                (per / asin / "amazon.json").write_text(
                    json.dumps({"item": {"jan_code": jan}}), encoding="utf-8")
            (arts / "x-B0AAAAAAAA.seo.json").write_text("{}", encoding="utf-8")
            self.assertEqual(fso.collect_targets(arts, per), {"B0AAAAAAAA": "4900000000001"})

    def test_collect_targets_newest_article_first(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            arts, per = root / "articles", root / "per_asin"
            arts.mkdir()
            for asin, date in (("B0AAAAAAAA", "2026-09-01"), ("B0BBBBBBBB", "2026-11-20"),
                               ("B0CCCCCCCC", "2026-10-01"), ("B0DDDDDDDD", "Nov 2026")):
                (arts / f"x-{asin}.json").write_text(json.dumps({"date": date}), encoding="utf-8")
                (per / asin).mkdir(parents=True)
                (per / asin / "amazon.json").write_text(
                    json.dumps({"item": {"jan_code": "4900000000001"}}), encoding="utf-8")
            targets = fso.collect_targets(arts, per)
            self.assertEqual(list(targets), ["B0BBBBBBBB", "B0CCCCCCCC", "B0AAAAAAAA"])
            # まだ確かめていないものは、この並び (記事の新しい順) のまま取る
            self.assertEqual(fso.pick({}, "toysrus_rakuten", targets, NOW, 2, fso.MIN_AGE_DAYS),
                             ["B0BBBBBBBB", "B0CCCCCCCC"])


class RunTest(unittest.TestCase):
    def test_intervals_and_records(self):
        clock = {"t": 0.0}

        def fake_check(key, jan, keys):
            if key == "toysrus_rakuten":
                return fso.FOUND, {"price": 7480, "url": TR_URL}
            return fso.FAILED, None

        def sleep(s):
            clock["t"] += s

        records = {"B": {"toysrus_yahoo": {"status": "found", "price": 1}}}
        prog = {"applied": 0}
        with patch.object(fso, "check", side_effect=fake_check):
            stats = fso.run({"toysrus_rakuten": ["A", "B"], "toysrus_yahoo": ["B"]}, records,
                            {"A": "1", "B": "2"}, KEYS, deadline=999, dry_run=False,
                            now_iso=NOW.isoformat(), clock=lambda: clock["t"], sleep=sleep,
                            progress=prog)
        self.assertEqual(records["A"]["toysrus_rakuten"]["price"], 7480)
        self.assertEqual(records["B"]["toysrus_yahoo"], {"status": "found", "price": 1})
        self.assertEqual(prog["applied"], 2)
        self.assertEqual(stats["toysrus_yahoo"][fso.FAILED], 1)
        self.assertGreaterEqual(clock["t"], rsp.RAKUTEN_INTERVAL)

    def test_rate_limited_stops_store(self):
        with patch.object(fso, "check", side_effect=rsp.RateLimited("x")):
            stats = fso.run({"toysrus_rakuten": ["A", "B"]}, {}, {"A": "1", "B": "2"}, KEYS,
                            deadline=999, dry_run=False, now_iso=NOW.isoformat(),
                            clock=lambda: 0.0, sleep=lambda s: None)
        self.assertEqual(stats["toysrus_rakuten"], {"rate_limited": 1, "left": 1})

    def test_dry_run_changes_nothing(self):
        records = {}
        with patch.object(fso, "check", return_value=(fso.FOUND, {"price": 1, "url": TR_URL})):
            fso.run({"toysrus_rakuten": ["A"]}, records, {"A": "1"}, KEYS, deadline=999,
                    dry_run=True, now_iso=NOW.isoformat(), clock=lambda: 0.0, sleep=lambda s: None)
        self.assertEqual(records, {})


class MainTest(unittest.TestCase):
    def _setup(self, root: pathlib.Path):
        (root / "data/articles").mkdir(parents=True)
        per = root / "data/raw/per_asin/B0AAAAAAAA"
        per.mkdir(parents=True)
        (per / "amazon.json").write_text(json.dumps({"item": {"jan_code": "4900000000001"}}),
                                         encoding="utf-8")
        (root / "data/articles/x-B0AAAAAAAA.json").write_text(json.dumps({"date": "2026-11-01"}),
                                                              encoding="utf-8")
        for f in ("rakuten_matched.json", "yahoo_matched.json"):
            (root / "data/raw" / f).write_text(json.dumps({"items": []}), encoding="utf-8")

    def _main(self, root, argv, check_ret):
        env = {"RAKUTEN_APP_ID": "a", "RAKUTEN_ACCESS_KEY": "k", "YAHOO_CLIENT_ID": "y"}
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with patch.dict(os.environ, env), \
                 patch.object(fso, "check", return_value=check_ret) as c, \
                 patch.object(fso.time, "sleep"):
                fso.main(argv)
            return c
        finally:
            os.chdir(cwd)

    def test_write_then_same_day_skips(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            self._setup(root)
            self._main(root, ["--write"], (fso.FOUND, {"price": 7480, "url": TR_URL}))
            out = json.loads((root / "data/raw/store_offers.json").read_text(encoding="utf-8"))
            self.assertEqual(out["items"]["B0AAAAAAAA"]["toysrus_rakuten"]["status"], "found")
            c = self._main(root, ["--write"], (fso.FOUND, {"price": 7480, "url": TR_URL}))
            c.assert_not_called()

    def test_write_drops_records_outside_targets_and_keeps_others(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            self._setup(root)
            iso = NOW.isoformat()
            prev = {"items": {
                "B0AAAAAAAA": {"toysrus_yahoo": {"status": "found", "price": 1, "jan": "4900000000001",
                                                 "checked_at": iso}},
                "B0ZZZZZZZZ": {"toysrus_rakuten": {"status": "found", "price": 1}},
            }}
            (root / "data/raw/store_offers.json").write_text(json.dumps(prev), encoding="utf-8")
            self._main(root, ["--write"], (fso.FOUND, {"price": 7480, "url": TR_URL}))
            items = json.loads((root / "data/raw/store_offers.json").read_text(encoding="utf-8"))["items"]
            self.assertEqual(set(items), {"B0AAAAAAAA"})
            self.assertEqual(items["B0AAAAAAAA"]["toysrus_rakuten"]["price"], 7480)
            # Yahoo は最近確かめたので呼ばず、記録はそのまま
            self.assertEqual(items["B0AAAAAAAA"]["toysrus_yahoo"]["price"], 1)

    def test_all_failed_and_dry_run_write_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            self._setup(root)
            self._main(root, ["--write"], (fso.FAILED, None))
            self.assertFalse((root / "data/raw/store_offers.json").exists())
            self._main(root, [], (fso.FOUND, {"price": 7480, "url": TR_URL}))
            self.assertFalse((root / "data/raw/store_offers.json").exists())


class StoreRowsTest(unittest.TestCase):
    def _rec(self, **kw):
        r = {"status": "found", "price": 7480, "availability": "instock", "url": TR_URL,
             "checked_at": (NOW - timedelta(days=3)).isoformat()}
        r.update(kw)
        return r

    def test_found_and_fresh_rows(self):
        rows = wtb.build_store_rows(
            {"toysrus_rakuten": self._rec(),
             "toysrus_yahoo": self._rec(url=TY_URL, availability="outofstock")}, [], now=NOW)
        self.assertEqual([r["site"] for r in rows],
                         ["トイザらス（楽天市場店）", "トイザらス（Yahoo!ショッピング店）"])
        self.assertEqual([r["state_label"] for r in rows], ["在庫あり", "在庫切れ"])
        self.assertEqual(rows[0]["observed_at_label"], "2026-11-25 21:00")

    def test_missing_stale_and_no_price_are_hidden(self):
        offers = {"toysrus_rakuten": self._rec(status="missing"),
                  "toysrus_yahoo": self._rec(url=TY_URL,
                                             checked_at=(NOW - timedelta(days=15)).isoformat())}
        self.assertEqual(wtb.build_store_rows(offers, [], now=NOW), [])
        self.assertEqual(wtb.build_store_rows({"toysrus_rakuten": self._rec(price=0)}, [], now=NOW), [])
        self.assertEqual(wtb.build_store_rows(None, [], now=NOW), [])

    def test_no_duplicate_when_matched_row_is_same_store(self):
        existing = [{"url": "https://hb.afl.rakuten.co.jp/x/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Ftoysrus%2F999%2F"},
                    {"url": "https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1&pid=2&vc_url="
                            "https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Ftoysrus-babierus%2F9.html"}]
        offers = {"toysrus_rakuten": self._rec(), "toysrus_yahoo": self._rec(url=TY_URL)}
        self.assertEqual(wtb.build_store_rows(offers, existing, now=NOW), [])
        # 横断検索の行が検索リンク (消えた・品質で隠れた) なら店名の行を出す
        search = [{"url": "https://search.rakuten.co.jp/search/mall/x/"}]
        self.assertEqual(len(wtb.build_store_rows(offers, search, now=NOW)), 2)

    def test_14_day_boundary_and_naive_now(self):
        rec = self._rec(checked_at=(NOW - timedelta(days=14)).isoformat())
        self.assertEqual(len(wtb.build_store_rows({"toysrus_rakuten": rec}, [], now=NOW)), 1)
        naive = NOW.replace(tzinfo=None)
        self.assertEqual(len(wtb.build_store_rows({"toysrus_rakuten": rec}, [], now=naive)), 1)

    def test_stock_block_without_offers_is_unchanged(self):
        import stock_status
        obs = stock_status.StockObservation(asin="B0AAAAAAAA", state=stock_status.STATE_IN_STOCK,
                                            observed_at=NOW.isoformat(), raw_avail="在庫あり。",
                                            remaining=None, price=7480)
        opts = {"amazon": {"available": True, "price": 7480, "url": "https://amazon.co.jp/dp/x"}}
        a = wtb.build_stock_block("X", obs, opts)
        b = wtb.build_stock_block("X", obs, opts, store_offers={"toysrus_rakuten": self._rec()}, now=NOW)
        self.assertEqual(len(a["rows"]), 3)
        self.assertEqual(len(b["rows"]), 4)
        self.assertEqual(b["rows"][3]["url"], TR_URL)


if __name__ == "__main__":
    unittest.main()
