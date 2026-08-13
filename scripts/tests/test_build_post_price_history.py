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

    def test_chart_window_equals_full_history(self):
        """#5120: 直近12点への切り詰めを撤廃。グラフの窓 = 文章の窓 (全履歴)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [_rec(30 - i, 1000 + i * 10) for i in range(20)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            ok = _attach_price_history(data, root)
            self.assertTrue(ok)
            ph = data["price_history"]
            self.assertEqual(len(ph["points"]), 20)
            self.assertEqual(ph["points"][-1]["price"], recs[-1]["price"])
            # spark のセグメントに含まれる頂点数も全20点ぶんの経路を覆う
            # (step-after で各観測につき水平+垂直の2頂点を追加するので、
            # 単純な点数一致ではなく「最初と最後の座標が全区間を覆う」ことを見る)
            all_points_str = " ".join(seg["points"] for seg in ph["spark"]["segments"])
            coords = [tuple(map(float, xy.split(","))) for xy in all_points_str.split(" ")]
            xs = [c[0] for c in coords]
            self.assertAlmostEqual(min(xs), 2.0, places=1)
            self.assertAlmostEqual(max(xs), 298.0, places=1)

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


class SparkGeometryTests(unittest.TestCase):
    """#5120: x を経過時間に比例させ、階段線 + 未観測ギャップの破線分割を検証する。"""

    def test_x_is_time_proportional_not_equally_spaced(self):
        # 0日, 20日, 40日 (span=40日) の3点。中点が 1/2 でなく "1/4" 相当の
        # 位置に来るよう、間隔を極端に不揃いにする: 0日, 30日, 40日 (span=40日)。
        # 30日時点は 30/40 = 0.75 -> x = 2 + 0.75*296 = 224.0。
        # 等間隔レイアウトなら中点の index は 1/2 -> x = 150.0 になるはずなので、
        # 224.0 ではなく 150.0 に近ければ現行の等間隔バグ、224.0 に近ければ正しい。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(40, 1000), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            xs = [float(p.split(",")[0]) for p in all_xy]
            # 等間隔 (150.0) には無く、時間比例 (224.0 近傍) の x が現れる
            self.assertTrue(any(abs(x - 224.0) < 1.0 for x in xs), xs)
            self.assertFalse(any(abs(x - 150.0) < 1.0 for x in xs), xs)

    def test_quarter_span_point_lands_near_expected_x(self):
        # 4点を 0, 10, 30, 40日前に配置 (span=40日)。10日前の点は
        # 経過時間 30/40 = 0.75 -> x = 2 + 0.75*296 = 224.0 に来るはず
        # (等間隔なら index 2/3 -> x ≈ 199.3 になる)。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(40, 1000), _rec(30, 1050), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            xs = sorted(set(float(p.split(",")[0]) for p in all_xy))
            self.assertTrue(any(abs(x - 224.0) < 1.0 for x in xs), xs)

    def test_all_equal_prices_give_y_24_and_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1500), _rec(10, 1500), _rec(0, 1500)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            ys = [float(p.split(",")[1]) for p in all_xy]
            self.assertTrue(all(y == 24.0 for y in ys), ys)

    def test_long_gap_produces_unobserved_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 0-5日: 密な観測。5日->25日: 20日ギャップ (>14日, 未観測)。
            _write_jsonl(root, "B1", [
                _rec(25, 1000), _rec(20, 1050),
                _rec(5, 1100), _rec(0, 1200),
            ])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            observed_flags = [s["observed"] for s in segs]
            self.assertIn(False, observed_flags)
            self.assertIn(True, observed_flags)

    def test_normal_interval_stays_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(14, 1050), _rec(7, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            self.assertTrue(all(s["observed"] for s in segs))
            self.assertEqual(len(segs), 1)

    def test_step_after_vertex_exists_before_price_change(self):
        # step-after: 価格が変わる直前に、同じ y (直前の価格) を保ったまま
        # 新しい x まで水平移動する頂点が入る。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1000), _rec(0, 2000)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            coords = [tuple(map(float, p.split(","))) for p in all_xy]
            # 最後の観測点 (最新, price=2000 -> y=4.0) の x と同じ x に、
            # 直前の価格 (1000 -> y=44.0) を保持した頂点があるはず
            last_x = coords[-1][0]
            self.assertTrue(
                any(abs(x - last_x) < 0.01 and abs(y - 44.0) < 0.01 for x, y in coords[:-1]),
                coords,
            )


if __name__ == "__main__":
    unittest.main()
