"""Unit tests for scripts/price_history.py (#2953 C案・訂正版)。

append_price_point の挙動を検証する:
    - 新規追記
    - 価格変化時は即追記 (経過日数に関わらず)
    - 同一価格・6日未満はスキップ (重複抑制)
    - 同一価格・6日超は追記
    - 不正な price (非 int / 0 以下 / bool) はスキップ
    - 壊れた既存行があっても落ちずに追記できる

#5130 で追加:
    - 在庫メッセージがある価格なし観測は price=null の行として残す
    - 根拠 (在庫メッセージ) の無い欠測は従来どおりスキップ
    - 在庫切れの継続は 6 日で間引き、状態変化は即記録
    - 新しい行が既存の読み側の集計に混ざらない
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

from price_history import append_price_point  # type: ignore[import-not-found]


def _read_records(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 壊れた既存行はテスト側でも読み飛ばす (本体と同じ寛容さ)
    return out


class AppendPricePointTests(unittest.TestCase):
    def test_appends_new_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ok = append_price_point(str(root), "b1", "amazon", 2659, "在庫あり")
            self.assertTrue(ok)
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["price"], 2659)
            self.assertEqual(recs[0]["source"], "amazon")
            self.assertEqual(recs[0]["availability"], "在庫あり")
            self.assertIn("ts", recs[0])

    def test_filename_uses_uppercase_asin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            append_price_point(str(root), "b0abc12345", "amazon", 1000, None)
            self.assertTrue((root / "B0ABC12345.jsonl").exists())

    def test_null_availability_when_falsy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            append_price_point(str(root), "B1", "amazon", 1000, "")
            recs = _read_records(root / "B1.jsonl")
            self.assertIsNone(recs[0]["availability"])

    def test_price_change_appends_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", 1000, None, ts=now)
            ok = append_price_point(
                str(root), "B1", "amazon", 1200, None,
                ts=now + timedelta(minutes=5))
            self.assertTrue(ok)
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[1]["price"], 1200)

    def test_same_price_under_six_days_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", 1000, None, ts=now)
            ok = append_price_point(
                str(root), "B1", "amazon", 1000, None,
                ts=now + timedelta(days=5))
            self.assertFalse(ok)
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual(len(recs), 1)

    def test_same_price_over_six_days_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", 1000, None, ts=now)
            ok = append_price_point(
                str(root), "B1", "amazon", 1000, None,
                ts=now + timedelta(days=6, minutes=1))
            self.assertTrue(ok)
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual(len(recs), 2)

    def test_invalid_price_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertFalse(append_price_point(str(root), "B1", "amazon", 0, None))
            self.assertFalse(append_price_point(str(root), "B1", "amazon", -50, None))
            self.assertFalse(append_price_point(str(root), "B1", "amazon", None, None))
            self.assertFalse(append_price_point(str(root), "B1", "amazon", "2659", None))
            self.assertFalse(append_price_point(str(root), "B1", "amazon", True, None))
            self.assertFalse((root / "B1.jsonl").exists())

    def test_missing_asin_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertFalse(append_price_point(str(root), "", "amazon", 1000, None))

    def test_survives_corrupted_existing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            root.mkdir(exist_ok=True)
            p = root / "B1.jsonl"
            p.write_text("not-json\n{\"broken\n", encoding="utf-8")
            ok = append_price_point(str(root), "B1", "amazon", 1000, None)
            self.assertTrue(ok)
            recs = _read_records(p)
            # 壊れた既存行はスキップされ、末尾に新規行のみ追加される
            self.assertEqual(recs[-1]["price"], 1000)

    def test_different_source_does_not_suppress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "rakuten", 1000, None, ts=now)
            ok = append_price_point(
                str(root), "B1", "amazon", 1000, None,
                ts=now + timedelta(minutes=1))
            self.assertTrue(ok)


class OutOfStockObservationTests(unittest.TestCase):
    """#5130: 価格が取れない日を 1 行も残さないと、階段線が「最後に価格が取れた日の値が
    今日まで続いた」と嘘をつく。在庫メッセージという根拠があるときだけ行を残す。
    """

    def test_out_of_stock_is_recorded_with_null_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ok = append_price_point(str(root), "B1", "amazon", None, "現在在庫切れです。")
            self.assertTrue(ok)
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual(len(recs), 1)
            self.assertIsNone(recs[0]["price"])
            self.assertEqual(recs[0]["availability"], "現在在庫切れです。")

    def test_missing_price_without_evidence_is_still_skipped(self):
        """在庫メッセージも無い欠測は API エラーと区別できない。捏造しない。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertFalse(append_price_point(str(root), "B1", "amazon", None, None))
            self.assertFalse(append_price_point(str(root), "B1", "amazon", None, "   "))
            self.assertFalse((root / "B1.jsonl").exists())

    def test_continued_out_of_stock_is_deduped_then_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", None, "現在在庫切れです。", ts=now)
            self.assertFalse(append_price_point(
                str(root), "B1", "amazon", None, "現在在庫切れです。",
                ts=now + timedelta(days=5)))
            self.assertTrue(append_price_point(
                str(root), "B1", "amazon", None, "現在在庫切れです。",
                ts=now + timedelta(days=7)))
            self.assertEqual(len(_read_records(root / "B1.jsonl")), 2)

    def test_availability_change_appends_immediately(self):
        """「在庫切れ」→「お取り扱いできません」は状態変化なので間引かない。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", None, "現在在庫切れです。", ts=now)
            self.assertTrue(append_price_point(
                str(root), "B1", "amazon", None, "この商品は現在お取り扱いできません。",
                ts=now + timedelta(hours=1)))

    def test_transitions_between_priced_and_out_of_stock_are_immediate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", 2827, "在庫あり", ts=now)
            # 価格あり → 在庫切れ
            self.assertTrue(append_price_point(
                str(root), "B1", "amazon", None, "現在在庫切れです。",
                ts=now + timedelta(hours=1)))
            # 在庫切れ → 価格あり (同じ価格に戻っても、状態が変わったので残す)
            self.assertTrue(append_price_point(
                str(root), "B1", "amazon", 2827, "在庫あり", ts=now + timedelta(hours=2)))
            recs = _read_records(root / "B1.jsonl")
            self.assertEqual([r["price"] for r in recs], [2827, None, 2827])

    def test_null_price_rows_are_inert_for_readers(self):
        """既存の読み側 3 本は price が正の int でない行を捨てる。新しい行が
        最安値や描画ゲートに混ざらないことを、実際の reader で確かめる (#5130)。
        """
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from build_post import _load_price_history_points  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            now = datetime.now(timezone.utc)
            append_price_point(str(root), "B1", "amazon", 2827, "在庫あり", ts=now)
            append_price_point(str(root), "B1", "amazon", None, "現在在庫切れです。",
                               ts=now + timedelta(days=1))
            points = _load_price_history_points(root, "B1")
            self.assertEqual([p["price"] for p in points], [2827])


if __name__ == "__main__":
    unittest.main()
