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
            # #5120 追補: プロット領域は x=52〜296 (左52pxはy軸ラベル用の余白)
            self.assertAlmostEqual(min(xs), 52.0, places=1)
            self.assertAlmostEqual(max(xs), 296.0, places=1)

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
        # プロット領域は x=52〜296 (幅244)。30日時点は 30/40 = 0.75 ->
        # x = 52 + 0.75*244 = 235.0。等間隔レイアウトなら中点の index は
        # 1/2 -> x = 52 + 0.5*244 = 174.0 になるはずなので、235.0 ではなく
        # 174.0 に近ければ現行の等間隔バグ、235.0 に近ければ正しい。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(40, 1000), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            xs = [float(p.split(",")[0]) for p in all_xy]
            # 等間隔 (174.0) には無く、時間比例 (235.0 近傍) の x が現れる
            self.assertTrue(any(abs(x - 235.0) < 1.0 for x in xs), xs)
            self.assertFalse(any(abs(x - 174.0) < 1.0 for x in xs), xs)

    def test_quarter_span_point_lands_near_expected_x(self):
        # 4点を 0, 10, 30, 40日前に配置 (span=40日)。10日前の点は
        # 経過時間 30/40 = 0.75 -> x = 52 + 0.75*244 = 235.0 に来るはず
        # (等間隔なら index 2/3 -> x ≈ 214.7 になる)。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(40, 1000), _rec(30, 1050), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            xs = sorted(set(float(p.split(",")[0]) for p in all_xy))
            self.assertTrue(any(abs(x - 235.0) < 1.0 for x in xs), xs)

    def test_all_equal_prices_give_y_mid_and_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1500), _rec(10, 1500), _rec(0, 1500)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            all_xy = " ".join(s["points"] for s in segs).split(" ")
            ys = [float(p.split(",")[1]) for p in all_xy]
            # プロット領域 y=8〜56 の中点は 32.0
            self.assertTrue(all(y == 32.0 for y in ys), ys)

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
            # 最後の観測点 (最新, price=2000 -> y=8.0) の x と同じ x に、
            # 直前の価格 (1000 -> y=56.0) を保持した頂点があるはず
            last_x = coords[-1][0]
            self.assertTrue(
                any(abs(x - last_x) < 0.01 and abs(y - 56.0) < 0.01 for x, y in coords[:-1]),
                coords,
            )


    def test_gap_vertical_move_is_observed_not_dashed(self):
        """#5120 追補: ギャップ辺の垂直移動 (新観測時点の実測変化) は破線に
        含めない。破線は「水平の未観測ホールド」だけを覆うべき。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 0-5日: 密な観測。5日->25日: 20日ギャップ (>14日)。ギャップの
            # 終端 (25日前=最古側) で価格が 1100 -> 1000 に実測変化する。
            _write_jsonl(root, "B1", [
                _rec(25, 1000), _rec(20, 1100),
                _rec(5, 1200), _rec(0, 1300),
            ])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            dashed = [s for s in segs if not s["observed"]]
            observed = [s for s in segs if s["observed"]]
            self.assertEqual(len(dashed), 1)
            dashed_coords = [tuple(map(float, p.split(","))) for p in dashed[0]["points"].split(" ")]
            # 破線セグメントは水平ホールドのみ = 全頂点の y が同一
            ys = {y for _, y in dashed_coords}
            self.assertEqual(len(ys), 1, dashed_coords)
            # 破線の終端 x と、いずれかの観測(実線)セグメントの先頭 x が
            # 一致する (垂直移動がそこから観測済みとして続く)
            dashed_end_x = dashed_coords[-1][0]
            observed_start_xs = [tuple(map(float, s["points"].split(" ")[0].split(",")))[0] for s in observed]
            self.assertIn(dashed_end_x, observed_start_xs)
            # 垂直移動 (y が変わる辺) が観測セグモートのどこかに存在する
            all_observed_coords = []
            for s in observed:
                all_observed_coords.append([tuple(map(float, p.split(","))) for p in s["points"].split(" ")])
            found_vertical_at_dashed_end = False
            for coords_list in all_observed_coords:
                for i in range(len(coords_list) - 1):
                    if coords_list[i][0] == dashed_end_x and coords_list[i][1] != coords_list[i + 1][1] \
                            and coords_list[i + 1][0] == dashed_end_x:
                        found_vertical_at_dashed_end = True
            self.assertTrue(found_vertical_at_dashed_end, all_observed_coords)

    def test_no_consecutive_duplicate_coords_in_any_segment(self):
        """#5120 追補: 価格が変わらない辺で step 頂点=実観測点となり、丸め後
        座標が直前と同一になる場合は追加しない (無駄なマークアップ削減)。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [_rec(30 - i, 1000 if i % 3 else 1000 + i) for i in range(15)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            segs = data["price_history"]["spark"]["segments"]
            for seg in segs:
                coords = [tuple(map(float, p.split(","))) for p in seg["points"].split(" ")]
                for i in range(len(coords) - 1):
                    self.assertNotEqual(coords[i], coords[i + 1], (seg, coords))
                # セグメントは最低2頂点 (単一頂点の polyline は出力しない)
                self.assertGreaterEqual(len(coords), 2)

    def test_y_labels_have_yen_formatted_max_and_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1680), _rec(10, 2000), _rec(0, 1900)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            y_labels = data["price_history"]["spark"]["y_labels"]
            self.assertEqual(len(y_labels), 2)
            texts = {label["text"] for label in y_labels}
            self.assertEqual(texts, {"¥1,680", "¥2,000"})
            for label in y_labels:
                self.assertEqual(label["anchor"], "end")
                self.assertEqual(label["x"], 48.0)

    def test_y_label_single_when_all_prices_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1500), _rec(10, 1500), _rec(0, 1500)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            y_labels = data["price_history"]["spark"]["y_labels"]
            self.assertEqual(len(y_labels), 1)
            self.assertEqual(y_labels[0]["text"], "¥1,500")

    def test_x_labels_are_oldest_and_newest_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            x_labels = data["price_history"]["spark"]["x_labels"]
            self.assertEqual(len(x_labels), 2)
            self.assertEqual(x_labels[0]["text"], recs[0]["ts"][:10])
            self.assertEqual(x_labels[0]["anchor"], "start")
            self.assertEqual(x_labels[1]["text"], recs[-1]["ts"][:10])
            self.assertEqual(x_labels[1]["anchor"], "end")

    def test_dots_match_observed_points_not_step_vertices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 価格が横ばい→ジャンプ: step-after で水平保持の中間頂点が入るが、
            # dots は実観測点 (3点) だけを持つべき。
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1000), _rec(0, 2000)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            dots = data["price_history"]["spark"]["dots"]
            self.assertEqual(len(dots), 3)

    def test_legend_absent_without_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(14, 1050), _rec(7, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            self.assertIsNone(data["price_history"]["spark"]["legend"])

    def test_legend_present_with_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(25, 1000), _rec(20, 1050), _rec(5, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            legend = data["price_history"]["spark"]["legend"]
            self.assertIsNotNone(legend)
            self.assertEqual(legend["text"], "破線＝未観測期間")


def _write_latest_json(path: pathlib.Path, items: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": "2026-08-13T00:00:00+00:00", "items": items,
                     "source": "creators-api", "stats": {}}, ensure_ascii=False),
        encoding="utf-8",
    )


class CheckedDailyTests(unittest.TestCase):
    """#5120 追補: latest.json (日次レーンの最新スナップショット) を読んで
    「今日も見た」かどうかを ``checked_daily`` / ``last_checked_date`` に
    反映する。latest.json 側の異常はすべて従来の週次表記へ静かにフォールバック
    する (例外で落とさない)。
    """

    def test_missing_latest_json_falls_back_to_weekly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"  # 作らない
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            self.assertFalse(data["price_history"]["checked_daily"])
            self.assertIsNone(data["price_history"]["last_checked_date"])

    def test_asin_present_in_latest_json_sets_checked_daily_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)])
            _write_latest_json(latest, {"B1": {"p": 1200, "ts": "2026-08-13T01:00:00+00:00"}})
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            self.assertTrue(data["price_history"]["checked_daily"])
            self.assertEqual(data["price_history"]["last_checked_date"], "2026-08-13")

    def test_asin_missing_from_latest_json_items_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)])
            _write_latest_json(latest, {"OTHERASIN": {"p": 500, "ts": "2026-08-13T01:00:00+00:00"}})
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            self.assertFalse(data["price_history"]["checked_daily"])
            self.assertIsNone(data["price_history"]["last_checked_date"])

    def test_corrupted_latest_json_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)])
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text("{not valid json", encoding="utf-8")
            data = {"product": {"asin": "B1"}}
            # 例外を投げず False にフォールバックすること自体がテスト対象
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            self.assertFalse(data["price_history"]["checked_daily"])
            self.assertIsNone(data["price_history"]["last_checked_date"])

    def test_no_latest_path_given_defaults_to_weekly(self):
        """後方互換: price_watch_latest_path 省略時は checked_daily=False。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            _write_jsonl(root, "B1", [_rec(20, 1000), _rec(10, 1100), _rec(0, 1200)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            self.assertFalse(data["price_history"]["checked_daily"])
            self.assertIsNone(data["price_history"]["last_checked_date"])


class ChangeCountTests(unittest.TestCase):
    """#5120 追補: change_count = 隣接する記録間で価格が変わった回数。

    「N回計測しました」ではなく「N回の値動きをとらえました」という、記録から
    厳密に言える主張だけを支える数値。records (jsonl の行数) とは別物。
    """

    def test_change_count_matches_number_of_price_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 1000 -> 1000 (変化なし) -> 1200 (変化) -> 1200 (変化なし) -> 1300 (変化)
            _write_jsonl(root, "B1", [
                _rec(20, 1000), _rec(15, 1000), _rec(10, 1200), _rec(5, 1200), _rec(0, 1300),
            ])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            self.assertEqual(data["price_history"]["change_count"], 2)

    def test_change_count_zero_when_all_prices_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_jsonl(root, "B1", [_rec(20, 1500), _rec(10, 1500), _rec(0, 1500)])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            self.assertEqual(data["price_history"]["change_count"], 0)

    def test_change_count_records_records_not_records_count(self):
        """records (jsonl 行数) が多くても change_count はそれと独立に数える。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 5レコードだが価格変化は1回だけ
            _write_jsonl(root, "B1", [
                _rec(20, 1000), _rec(16, 1000), _rec(12, 1000), _rec(6, 1000), _rec(0, 1100),
            ])
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            ph = data["price_history"]
            self.assertEqual(len(ph["points"]), 5)
            self.assertEqual(ph["change_count"], 1)
            self.assertNotEqual(ph["change_count"], len(ph["points"]))


class TableRowDedupeTests(unittest.TestCase):
    """#5120 追補: 週次/日次2レーンが同じ日に同じ価格を別時刻で記録すると、
    日付単位に丸めて出す表には完全に同じ行が2つ並ぶ (読者にはバグに見える)。
    表だけ間引き、SVG の座標・ドット・生の points は一切変更しない。
    """

    def test_duplicate_date_price_rows_are_deduped_in_table_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [
                _rec(20, 1000),
                _rec(10, 1200),
                _rec(10, 1200),  # 同日・同価格 (別レーンの重複を模す)
                _rec(0, 1300),
            ]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            ph = data["price_history"]
            self.assertEqual(len(ph["points"]), 4)  # 生の points は間引かない
            self.assertEqual(len(ph["table_rows"]), 3)  # table だけ間引く
            self.assertEqual(len(ph["spark"]["dots"]), 4)  # SVG のドットも間引かない

    def test_same_date_different_price_rows_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recs = [_rec(20, 1000), _rec(10, 1680), _rec(10, 1980), _rec(0, 1980)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))
            ph = data["price_history"]
            # 10日前の2行 (1680 -> 1980) は同日でも価格が違うので両方残る
            self.assertEqual(len(ph["table_rows"]), 4)


class GraphExtensionTests(unittest.TestCase):
    """#5120 追補2: 「最終確認日まで価格が変わっていない」ことが latest.json
    で確定しているときだけ、階段線を最終確認日まで水平に延長する。
    """

    def _future_ts(self, days: int = 2) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def test_extends_when_daily_and_price_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            future_ts = self._future_ts()
            _write_latest_json(latest, {"B1": {"p": 1500, "ts": future_ts}})
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            spark = data["price_history"]["spark"]
            self.assertEqual(spark["x_labels"][-1]["text"], future_ts[:10])
            all_xy = " ".join(s["points"] for s in spark["segments"]).split(" ")
            xs = [float(p.split(",")[0]) for p in all_xy]
            self.assertAlmostEqual(max(xs), 296.0, places=1)

    def test_no_extension_when_watch_price_is_null(self):
        """latest.json に p が無い (在庫切れ等、#5130 で別途起票済み) は延長しない。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            future_ts = self._future_ts()
            _write_latest_json(latest, {"B1": {"ts": future_ts}})  # p 無し
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            spark = data["price_history"]["spark"]
            self.assertEqual(spark["x_labels"][-1]["text"], recs[-1]["ts"][:10])

    def test_no_extension_when_watch_price_mismatches_last_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            future_ts = self._future_ts()
            _write_latest_json(latest, {"B1": {"p": 1600, "ts": future_ts}})  # 価格不一致
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            spark = data["price_history"]["spark"]
            self.assertEqual(spark["x_labels"][-1]["text"], recs[-1]["ts"][:10])

    def test_no_extension_when_not_checked_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root))  # latest_path 省略
            spark = data["price_history"]["spark"]
            self.assertEqual(spark["x_labels"][-1]["text"], recs[-1]["ts"][:10])

    def test_no_extension_when_last_checked_date_not_after_last_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            _write_latest_json(latest, {"B1": {"p": 1500, "ts": recs[-1]["ts"]}})  # 最終記録と同日
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            spark = data["price_history"]["spark"]
            self.assertEqual(spark["x_labels"][-1]["text"], recs[-1]["ts"][:10])

    def test_extension_adds_no_dots_or_table_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "price_history"
            latest = pathlib.Path(tmp) / "price_watch" / "latest.json"
            recs = [_rec(20, 1000), _rec(10, 1200), _rec(0, 1500)]
            _write_jsonl(root, "B1", recs)
            future_ts = self._future_ts()
            _write_latest_json(latest, {"B1": {"p": 1500, "ts": future_ts}})
            data = {"product": {"asin": "B1"}}
            self.assertTrue(_attach_price_history(data, root, price_watch_latest_path=latest))
            ph = data["price_history"]
            self.assertEqual(len(ph["spark"]["dots"]), 3)  # 観測点3件のまま (延長分は追加しない)
            self.assertEqual(len(ph["table_rows"]), 3)  # 表にも行を足さない


if __name__ == "__main__":
    unittest.main()
