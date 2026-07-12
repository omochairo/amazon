"""Unit tests for scripts/price_history.py (#2953 C案・訂正版)。

append_price_point の挙動を検証する:
    - 新規追記
    - 価格変化時は即追記 (経過日数に関わらず)
    - 同一価格・6日未満はスキップ (重複抑制)
    - 同一価格・6日超は追記
    - 不正な price (非 int / 0 以下 / bool) はスキップ
    - 壊れた既存行があっても落ちずに追記できる
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


if __name__ == "__main__":
    unittest.main()
