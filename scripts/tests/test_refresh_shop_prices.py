"""refresh_shop_prices.py (#12305) のテスト。

守ること:
  - 同じ商品ページだと確かめられたときだけ価格・在庫・確認日を更新する
  - 見つからないのが 2 回続いたら _gone_at。1 回では付けない
  - 一時的な失敗・429 の連続では何も変えない (429 はそのショップを打ち切る)
  - アフィリエイト URL を空で上書きしない
  - 古い順・記事の対象だけ・最近確かめたものは除く
  - 楽天と Yahoo の開始間隔を守る
  - 同じ日の 2 回目は何もしない。--dry-run は何も書かない
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

import refresh_shop_prices as rsp  # noqa: E402

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
KEYS = {"rakuten_app_id": "a", "rakuten_access_key": "k", "rakuten_aff_id": "aff",
        "yahoo_client_id": "y", "vc_sid": "1", "vc_pid": "2"}
R_URL = "https://hb.afl.rakuten.co.jp/hgc/x/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fshop%2F10001%2F"
B_URL = "https://hb.afl.rakuten.co.jp/hgc/x/?pc=https%3A%2F%2Fbooks.rakuten.co.jp%2Frb%2F123%2F"
Y_URL = ("https://ck.jp.ap.valuecommerce.com/servlet/referral?sid=1&pid=2&vc_url="
         "https%3A%2F%2Fstore.shopping.yahoo.co.jp%2Fmystore%2Fabc123.html")


def _resp(status, body=None, bad_json=False):
    r = MagicMock()
    r.status_code = status
    if bad_json:
        r.json.side_effect = ValueError("x")
    else:
        r.json.return_value = body
    return r


def _ichiba(**kw):
    e = {"matched_asin": "B0AAAAAAAA", "itemCode": "shop:10001", "url": R_URL, "price": 1000,
         "availability": "instock", "_refreshed_at": "2026-08-01T00:00:00+00:00"}
    e.update(kw)
    return e


class UrlTest(unittest.TestCase):
    def test_url_key_unwraps_affiliate_and_ignores_case(self):
        self.assertEqual(rsp.url_key(Y_URL), "store.shopping.yahoo.co.jp/mystore/abc123.html")
        # ホストは小文字にそろえるが、パスの大小文字は区別する (大小文字だけ違う別商品を同じとみなさない)
        self.assertEqual(rsp.url_key("https://STORE.shopping.yahoo.co.jp/mystore/abc123.html/"),
                         "store.shopping.yahoo.co.jp/mystore/abc123.html")
        self.assertNotEqual(rsp.url_key("https://store.shopping.yahoo.co.jp/mystore/ABC123.html"),
                            rsp.url_key(Y_URL))
        self.assertEqual(rsp.url_key(R_URL), "item.rakuten.co.jp/shop/10001")

    def test_unwrap_only_affiliate_hosts(self):
        plain = "https://item.rakuten.co.jp/shop/10001/?pc=https%3A%2F%2Fother.example%2F&m=x"
        self.assertEqual(rsp._unwrap(plain), plain)
        self.assertEqual(rsp.url_key(plain), "item.rakuten.co.jp/shop/10001")

    def test_yahoo_seller_id(self):
        self.assertEqual(rsp.yahoo_seller_id(Y_URL), "mystore")
        self.assertEqual(rsp.yahoo_seller_id("https://lohaco.yahoo.co.jp/store/h/item/x/"), "")


class MethodForTest(unittest.TestCase):
    def test_choose_by_clue(self):
        self.assertIs(rsp.method_for("rakuten", _ichiba()), rsp.check_rakuten_ichiba)
        books = {"itemCode": "", "jan_code": "9784000000000", "url": B_URL, "_match_method": "jan_books"}
        self.assertIs(rsp.method_for("rakuten", books), rsp.check_rakuten_books)
        # 商品名で照合した本は isbnjan で引き直さない (同じ本が返る保証がない)
        self.assertIsNone(rsp.method_for("rakuten", dict(books, _match_method="text")))
        self.assertIsNone(rsp.method_for("rakuten", {"itemCode": "", "jan_code": "", "url": B_URL}))
        self.assertIs(rsp.method_for("yahoo", {"url": Y_URL, "jan_code": "", "title": "t"}), rsp.check_yahoo)
        self.assertIsNone(rsp.method_for("yahoo", {"url": "https://lohaco.yahoo.co.jp/x", "jan_code": "1"}))

    def test_gone_entries_are_left_to_cross_search(self):
        self.assertIsNone(rsp.method_for("rakuten", _ichiba(_gone_at="2026-11-01T00:00:00+00:00")))


class CheckTest(unittest.TestCase):
    def _call(self, fn, entry, resp):
        with patch.object(rsp.requests, "get", return_value=resp) as g, \
             patch.object(rsp.time, "sleep"):
            out = fn(entry, KEYS)
        return out, g

    def test_ichiba_found_requests_item_code_with_availability_0(self):
        body = {"Items": [{"itemCode": "shop:10001", "itemName": "n", "itemPrice": 900,
                           "affiliateUrl": "https://aff", "availability": 0}]}
        (outcome, item), g = self._call(rsp.check_rakuten_ichiba, _ichiba(), _resp(200, body))
        self.assertEqual(outcome, rsp.FOUND)
        self.assertEqual((item["price"], item["availability"]), (900, "outofstock"))
        params = g.call_args.kwargs["params"]
        self.assertEqual((params["itemCode"], params["availability"], params["affiliateId"]),
                         ("shop:10001", 0, "aff"))

    def test_ichiba_404_and_empty_and_other_code_are_missing(self):
        for resp in (_resp(404, {"error": "not_found"}), _resp(200, {"Items": []}),
                     _resp(200, {"Items": [{"itemCode": "other:1", "itemPrice": 1}]})):
            (outcome, _), _g = self._call(rsp.check_rakuten_ichiba, _ichiba(), resp)
            self.assertEqual(outcome, rsp.MISSING)

    def test_failures_change_nothing(self):
        for resp in (_resp(500), _resp(401), _resp(403), _resp(200, bad_json=True)):
            (outcome, _), _g = self._call(rsp.check_rakuten_ichiba, _ichiba(), resp)
            self.assertEqual(outcome, rsp.FAILED)
        (outcome, _), _g = self._call(rsp.check_rakuten_ichiba, _ichiba(), _resp(400, {}))
        self.assertEqual(outcome, rsp.SKIPPED)

    def test_network_error_is_failure(self):
        with patch.object(rsp.requests, "get", side_effect=rsp.requests.exceptions.ConnectionError()):
            self.assertEqual(rsp.check_rakuten_ichiba(_ichiba(), KEYS)[0], rsp.FAILED)

    def test_429_twice_raises(self):
        with patch.object(rsp.requests, "get", side_effect=[_resp(429), _resp(429)]), \
             patch.object(rsp.time, "sleep") as sl:
            with self.assertRaises(rsp.RateLimited):
                rsp.check_rakuten_ichiba(_ichiba(), KEYS)
        sl.assert_called_once_with(rsp.RATE_LIMIT_WAIT)

    def test_books_matches_by_url(self):
        entry = {"itemCode": "", "jan_code": "9784000000000", "url": B_URL}
        hit = {"Items": [{"title": "本", "itemPrice": 700, "affiliateUrl":
                          "https://hb.afl.rakuten.co.jp/hgc/y/?pc=https%3A%2F%2Fbooks.rakuten.co.jp%2Frb%2F123%2F"}]}
        (outcome, item), g = self._call(rsp.check_rakuten_books, entry, _resp(200, hit))
        self.assertEqual((outcome, item["price"]), (rsp.FOUND, 700))
        self.assertEqual(g.call_args.kwargs["params"]["isbnjan"], "9784000000000")
        other = {"Items": [{"title": "別", "itemPrice": 1, "itemUrl": "https://books.rakuten.co.jp/rb/999/"}]}
        (outcome, _), _g = self._call(rsp.check_rakuten_books, entry, _resp(200, other))
        self.assertEqual(outcome, rsp.MISSING)

    def test_yahoo_text_match_with_jan_searches_by_title(self):
        # JAN はあるが照合は商品名 (JAN の検索が 0 件だった)。JAN で引くと 0 件で「消えた」と誤る
        entry = {"url": Y_URL, "jan_code": "4900000000001", "title": "木のつみき", "_match_method": "text"}
        (_outcome, _item), g = self._call(rsp.check_yahoo, entry, _resp(200, {"hits": []}))
        params = g.call_args.kwargs["params"]
        self.assertNotIn("jan_code", params)
        self.assertEqual(params["query"], "木のつみき")

    def test_yahoo_uses_seller_and_jan_and_matches_url(self):
        entry = {"url": Y_URL, "jan_code": "4900000000001", "title": "t", "_match_method": "jan"}
        body = {"hits": [
            {"url": "https://store.shopping.yahoo.co.jp/mystore/other.html", "price": 1},
            {"url": "https://store.shopping.yahoo.co.jp/mystore/abc123.html", "price": 880,
             "inStock": False, "name": "n", "shipping": {"code": 2}},
        ]}
        (outcome, item), g = self._call(rsp.check_yahoo, entry, _resp(200, body))
        self.assertEqual(outcome, rsp.FOUND)
        self.assertEqual((item["price"], item["availability"], item["free_shipping"]), (880, "outofstock", True))
        self.assertTrue(item["url"].startswith(rsp.fcs.VC_REFERRAL_BASE))
        params = g.call_args.kwargs["params"]
        self.assertEqual((params["seller_id"], params["jan_code"]), ("mystore", "4900000000001"))
        self.assertNotIn("in_stock", params)

    def test_yahoo_without_jan_searches_title_in_store(self):
        entry = {"url": Y_URL, "jan_code": "", "title": "木のつみき"}
        (outcome, _), g = self._call(rsp.check_yahoo, entry, _resp(200, {"hits": []}))
        self.assertEqual(outcome, rsp.MISSING)
        params = g.call_args.kwargs["params"]
        self.assertEqual(params["query"], "木のつみき")
        self.assertNotIn("jan_code", params)


class ApplyTest(unittest.TestCase):
    def test_found_updates_and_keeps_url_when_empty(self):
        e = _ichiba(_recheck_misses=1)
        rsp.apply_result(e, rsp.FOUND, {"price": 800, "availability": "outofstock", "url": ""}, "T")
        self.assertEqual((e["price"], e["availability"], e["url"], e["_refreshed_at"]),
                         (800, "outofstock", R_URL, "T"))
        self.assertNotIn("_recheck_misses", e)

    def test_gone_after_two_misses(self):
        e = _ichiba()
        self.assertEqual(rsp.apply_result(e, rsp.MISSING, None, "T1"), "missing(1)")
        self.assertNotIn("_gone_at", e)
        self.assertEqual(rsp.apply_result(e, rsp.MISSING, None, "T2"), "gone")
        self.assertEqual(e["_gone_at"], "T2")
        self.assertEqual(e["_refreshed_at"], "2026-08-01T00:00:00+00:00")  # 確認日は進めない

    def test_found_without_price_changes_nothing(self):
        for item in ({"price": 0}, {"price": None}, {"title": "x"}):
            e = _ichiba()
            before = dict(e)
            self.assertEqual(rsp.apply_result(e, rsp.FOUND, item, "T"), "no_price")
            self.assertEqual(e, before)

    def test_found_can_turn_flags_false(self):
        e = _ichiba(free_shipping=True, asuraku=True)
        rsp.apply_result(e, rsp.FOUND, {"price": 800, "free_shipping": False, "asuraku": False}, "T")
        self.assertEqual((e["free_shipping"], e["asuraku"]), (False, False))

    def test_failed_changes_nothing(self):
        e = _ichiba()
        before = dict(e)
        rsp.apply_result(e, rsp.FAILED, None, "T")
        self.assertEqual(e, before)


class PickTest(unittest.TestCase):
    def test_oldest_first_targets_only_skip_recent(self):
        entries = {
            "B0NEW00001": _ichiba(_refreshed_at=(NOW - timedelta(days=1)).isoformat()),
            "B0OLD00001": _ichiba(_refreshed_at="2026-01-01T00:00:00+00:00"),
            "B0MID00001": _ichiba(_refreshed_at="2026-06-01T00:00:00+00:00"),
            "B0NONE0001": _ichiba(_refreshed_at=None),
            "B0NOTTARGT": _ichiba(_refreshed_at="2025-01-01T00:00:00+00:00"),
        }
        targets = {"B0NEW00001", "B0OLD00001", "B0MID00001", "B0NONE0001"}
        got = rsp.pick(entries, "rakuten", targets, NOW, limit=10, min_age_days=6)
        self.assertEqual(got, ["B0NONE0001", "B0OLD00001", "B0MID00001"])
        self.assertEqual(rsp.pick(entries, "rakuten", targets, NOW, limit=2, min_age_days=6),
                         ["B0NONE0001", "B0OLD00001"])

    def test_default_limit_is_a_seventh(self):
        entries = {f"B0{i:08d}": _ichiba() for i in range(15)}
        self.assertEqual(rsp.default_limit(entries, "rakuten", set(entries)), 3)


class RunTest(unittest.TestCase):
    def _clock(self):
        t = {"now": 0.0}
        return t, (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))

    def test_intervals_are_kept_per_shop(self):
        t, clock, sleep = self._clock()
        starts = []

        def fake(shop):
            def _f(entry, keys):
                starts.append((shop, t["now"]))
                return rsp.FOUND, {"price": 1}
            return _f

        entries = {"rakuten": {f"R{i}": {"itemCode": "x", "url": R_URL} for i in range(3)},
                   "yahoo": {f"Y{i}": {"url": Y_URL, "jan_code": "1"} for i in range(2)}}
        queues = {"rakuten": list(entries["rakuten"]), "yahoo": list(entries["yahoo"])}
        with patch.object(rsp, "method_for", side_effect=lambda shop, e: fake(shop)):
            stats = rsp.run(queues, entries, KEYS, deadline=1e9, dry_run=False, now_iso="T",
                            clock=clock, sleep=sleep)
        r = [s for shop, s in starts if shop == "rakuten"]
        y = [s for shop, s in starts if shop == "yahoo"]
        self.assertTrue(all(b - a >= rsp.RAKUTEN_INTERVAL - 1e-9 for a, b in zip(r, r[1:])))
        self.assertTrue(all(b - a >= rsp.YAHOO_INTERVAL - 1e-9 for a, b in zip(y, y[1:])))
        self.assertEqual(stats["rakuten"]["found"], 3)
        self.assertEqual(stats["yahoo"]["found"], 2)

    def test_interval_counts_from_end_of_slow_call(self):
        # 429 → 65 秒待ってやり直した呼び出しの直後に、次を間隔なしで投げない
        t, clock, sleep = self._clock()
        starts = []

        def slow(entry, keys):
            starts.append(t["now"])
            if len(starts) == 1:
                t["now"] += rsp.RATE_LIMIT_WAIT  # 中で待ってやり直した
            return rsp.FOUND, {"price": 1}

        entries = {"rakuten": {"R1": {}, "R2": {}}}
        queues = {"rakuten": ["R1", "R2"]}
        with patch.object(rsp, "method_for", side_effect=lambda s, e: slow):
            rsp.run(queues, entries, KEYS, deadline=1e9, dry_run=False, now_iso="T",
                    clock=clock, sleep=sleep)
        self.assertGreaterEqual(starts[1] - (starts[0] + rsp.RATE_LIMIT_WAIT), rsp.RAKUTEN_INTERVAL - 1e-9)

    def test_rate_limited_shop_stops_other_continues(self):
        t, clock, sleep = self._clock()

        def fn(shop):
            def _f(entry, keys):
                if shop == "yahoo":
                    raise rsp.RateLimited("y")
                return rsp.FOUND, {"price": 1}
            return _f

        entries = {"rakuten": {"R1": {}, "R2": {}}, "yahoo": {"Y1": {}, "Y2": {}}}
        queues = {"rakuten": ["R1", "R2"], "yahoo": ["Y1", "Y2"]}
        with patch.object(rsp, "method_for", side_effect=lambda shop, e: fn(shop)):
            stats = rsp.run(queues, entries, KEYS, deadline=1e9, dry_run=False, now_iso="T",
                            clock=clock, sleep=sleep)
        self.assertEqual(stats["rakuten"]["found"], 2)
        self.assertEqual(stats["yahoo"]["rate_limited"], 1)
        self.assertEqual(stats["yahoo"]["left"], 1)

    def test_deadline_leaves_rest(self):
        t, clock, sleep = self._clock()
        entries = {"rakuten": {f"R{i}": {} for i in range(5)}}
        queues = {"rakuten": list(entries["rakuten"])}
        with patch.object(rsp, "method_for", side_effect=lambda s, e: (lambda en, k: (rsp.FOUND, {"price": 1}))):
            stats = rsp.run(queues, entries, KEYS, deadline=2.5, dry_run=False, now_iso="T",
                            clock=clock, sleep=sleep)
        self.assertEqual(stats["rakuten"]["found"], 3)
        self.assertEqual(stats["rakuten"]["left"], 2)


class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.raw = root / "data" / "raw"
        (self.raw / "per_asin").mkdir(parents=True)
        (root / "data" / "articles").mkdir(parents=True)
        (root / "data" / "articles" / "B0AAAAAAAA.json").write_text(
            json.dumps({"product": {"name": "x"}}), encoding="utf-8")
        (self.raw / "amazon.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        (self.raw / "rakuten_matched.json").write_text(json.dumps({"items": [_ichiba()]}), encoding="utf-8")
        (self.raw / "yahoo_matched.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        self._cwd = os.getcwd()
        os.chdir(root)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _main(self, *argv, resp=None):
        body = {"Items": [{"itemCode": "shop:10001", "itemPrice": 777, "affiliateUrl": "https://aff2"}]}
        resp = resp or _resp(200, body)
        env = {"RAKUTEN_APP_ID": "a", "RAKUTEN_ACCESS_KEY": "k", "YAHOO_CLIENT_ID": ""}
        with patch.dict(os.environ, env), \
             patch.object(rsp.requests, "get", return_value=resp) as g, \
             patch.object(rsp.time, "sleep"):
            rsp.main(list(argv))
        return g

    def _price(self):
        return json.loads((self.raw / "rakuten_matched.json").read_text(encoding="utf-8"))["items"][0]["price"]

    def test_default_is_dry_run_and_writes_nothing(self):
        g = self._main()
        self.assertEqual(g.call_count, 1)
        self.assertEqual(self._price(), 1000)
        self.assertFalse((self.raw / rsp.STATE_FILE).exists())

    def test_writes_once_per_day(self):
        self._main("--write")
        self.assertEqual(self._price(), 777)
        state = json.loads((self.raw / rsp.STATE_FILE).read_text(encoding="utf-8"))
        self.assertIn("last_run_jst", state)
        first = state["first_write_at"]
        g = self._main("--write")  # 同じ日の 2 回目
        self.assertEqual(g.call_count, 0)
        g = self._main("--write", "--force", "--min-age-days", "0")
        self.assertEqual(g.call_count, 1)
        state = json.loads((self.raw / rsp.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["first_write_at"], first)  # 動き始めた時刻は上書きしない

    def test_broken_files_do_not_crash(self):
        (self.raw / "rakuten_matched.json").write_text("{broken", encoding="utf-8")
        (self.raw / rsp.STATE_FILE).write_text("[]", encoding="utf-8")
        g = self._main("--write")
        self.assertEqual(g.call_count, 0)
        self.assertEqual((self.raw / "rakuten_matched.json").read_text(encoding="utf-8"), "{broken")

    def test_run_that_checks_nothing_leaves_no_record(self):
        self._main("--write", resp=_resp(503))
        self.assertEqual(self._price(), 1000)
        self.assertFalse((self.raw / rsp.STATE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
