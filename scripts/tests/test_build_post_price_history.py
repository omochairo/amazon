"""Unit tests for #2953 C案 (訂正版) — build_post の価格記録描画ゲート。

_attach_price_history はゲート (有効点 3 点以上 かつ 最古〜最新スパン 14 日以上)
を通ったときだけ ``data["price_history"]`` を添付する。ゲート未達では
``data`` を一切変更しない (テンプレの ``{% if price_history %}`` が
コンテキスト無しで何も描画しないことに依拠する)。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import _attach_price_history  # type: ignore[import-not-found]


def _write_jsonl(root: pathlib.Path, asin: str, records: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{asin.upper()}.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rec(days_ago: int, price: int, source: str = "amazon", availability=None) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "source": source, "price": price, "availability": availability}


class GateTests(unittest.TestCase):
    def test_no_file_no_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, pathlib.Path(tmp))
            self.assertFalse(ok)
            self.assertNotIn("price_history", data)

    def test_no_asin_no_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"product": {}}
            ok = _attach_price_history(data, pathlib.Path(tmp))
            self.assertFalse(ok)
            self.assertNotIn("price_history", data)

    def test_too_few_points_no_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(1, 1200)])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertFalse(ok)
            self.assertNotIn("price_history", data)

    def test_span_too_short_no_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(5, 1000), _rec(3, 1100), _rec(1, 1200)])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertFalse(ok)
            self.assertNotIn("price_history", data)

    def test_gate_passes_and_context_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [
                _rec(20, 2659),
                _rec(13, 2400),  # min
                _rec(6, 2800),   # max
                _rec(0, 2700),   # latest
            ])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            ph = data["price_history"]
            self.assertEqual(ph["min_price"], 2400)
            self.assertEqual(ph["max_price"], 2800)
            self.assertEqual(ph["latest_price"], 2700)
            self.assertGreaterEqual(ph["span_days"], 14)
            self.assertEqual(len(ph["points"]), 4)
            # points は ts 昇順 (古い→新しい)
            self.assertEqual(ph["points"][-1]["price"], 2700)
            # latest (2700) > min (2400) なので過去最安値ではない
            self.assertFalse(ph["all_time_low"])

    def test_all_time_low_true_when_latest_is_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [
                _rec(20, 2659),
                _rec(13, 2800),  # max
                _rec(6, 2500),
                _rec(0, 2400),   # latest == min (過去最安値)
            ])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            ph = data["price_history"]
            self.assertEqual(ph["min_price"], 2400)
            self.assertEqual(ph["latest_price"], 2400)
            self.assertTrue(ph["all_time_low"])

    def test_all_time_low_false_when_latest_above_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [
                _rec(20, 2659),
                _rec(13, 2400),  # min (過去にもっと安い点があった)
                _rec(6, 2500),
                _rec(0, 2600),   # latest > min
            ])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            ph = data["price_history"]
            self.assertEqual(ph["min_price"], 2400)
            self.assertEqual(ph["latest_price"], 2600)
            self.assertFalse(ph["all_time_low"])

    def test_ignores_non_amazon_source_and_invalid_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [
                _rec(20, 2000, source="rakuten"),
                _rec(19, 0),  # invalid price, ignored
                _rec(18, 2000),
                _rec(10, 2100),
                _rec(1, 2200),
            ])
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            # only 3 valid amazon points (18, 10, 1 days ago)
            self.assertEqual(len(data["price_history"]["points"]), 3)

    def test_points_capped_at_twelve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [_rec(30 - i, 1000 + i * 10) for i in range(20)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            self.assertEqual(len(data["price_history"]["points"]), 12)
            # 直近12点なので最新の値が末尾に残っているはず
            self.assertEqual(data["price_history"]["points"][-1]["price"], recs[-1]["price"])

    def test_corrupted_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            root.mkdir(parents=True, exist_ok=True)
            p = root / "B1.jsonl"
            lines = [
                "not-json",
                json.dumps(_rec(20, 1000)),
                json.dumps(_rec(13, 1100)),
                json.dumps(_rec(1, 1200)),
            ]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            self.assertEqual(len(data["price_history"]["points"]), 3)


class TwoLaneMergeTests(unittest.TestCase):
    """週次 (price_history) と日次 (price_watch) をマージして読む。

    2 レーンは dedupe が独立に効いて位相がずれ、実測でほぼ相補だった。片方だけを
    読むと min_price を高く見積もり、all_time_low を誤って True にしていた
    (実データ replay で 138 記事の min_price が下がり、120 記事で all_time_low
    の真偽が反転)。
    """

    def test_watch_lane_lowers_min_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = pathlib.Path(tmp) / "price_history"
            watch = pathlib.Path(tmp) / "price_watch"
            _write_jsonl(hist, "B1", [_rec(20, 2000), _rec(12, 1900), _rec(2, 1800)])
            _write_jsonl(watch, "B1", [_rec(15, 1500), _rec(6, 1700)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, hist, watch))
            self.assertEqual(data["price_history"]["min_price"], 1500)

    def test_watch_lane_falsifies_all_time_low(self):
        """週次だけ見ると「最新が過去最安」に見えるが、日次にもっと安い点がある。"""
        with tempfile.TemporaryDirectory() as tmp:
            hist = pathlib.Path(tmp) / "price_history"
            watch = pathlib.Path(tmp) / "price_watch"
            _write_jsonl(hist, "B1", [_rec(20, 2000), _rec(12, 1900), _rec(2, 1800)])
            only_hist = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(only_hist, hist))
            self.assertTrue(only_hist["price_history"]["all_time_low"])

            _write_jsonl(watch, "B1", [_rec(9, 1600)])
            merged = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(merged, hist, watch))
            self.assertFalse(merged["price_history"]["all_time_low"])

    def test_gate_passes_only_after_merge(self):
        """各レーン単独ではスパン 14 日に届かないが、合算すれば届く。"""
        with tempfile.TemporaryDirectory() as tmp:
            hist = pathlib.Path(tmp) / "price_history"
            watch = pathlib.Path(tmp) / "price_watch"
            _write_jsonl(hist, "B1", [_rec(20, 2000), _rec(18, 1950)])
            _write_jsonl(watch, "B1", [_rec(2, 1800)])
            self.assertFalse(_attach_price_history({"product": {"asin": "B1"}}, hist))
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, hist, watch))
            self.assertGreaterEqual(data["price_history"]["span_days"], 14)

    def test_identical_points_deduped_across_lanes(self):
        """両レーンに同一 (ts, price) があっても点数を水増ししない。"""
        with tempfile.TemporaryDirectory() as tmp:
            hist = pathlib.Path(tmp) / "price_history"
            watch = pathlib.Path(tmp) / "price_watch"
            recs = [_rec(20, 2000), _rec(12, 1900), _rec(2, 1800)]
            _write_jsonl(hist, "B1", recs)
            _write_jsonl(watch, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, hist, watch))
            self.assertEqual(len(data["price_history"]["points"]), 3)

    def test_missing_watch_dir_is_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = pathlib.Path(tmp) / "price_history"
            watch = pathlib.Path(tmp) / "price_watch"  # 作らない
            _write_jsonl(hist, "B1", [_rec(20, 2000), _rec(12, 1900), _rec(2, 1800)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, hist, watch))
            self.assertEqual(data["price_history"]["min_price"], 1800)


if __name__ == "__main__":
    unittest.main()
