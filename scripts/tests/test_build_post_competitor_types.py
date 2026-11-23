"""#11926 項目3: 🆚 似た商品の competitors.json 枠で、種類が違う候補を外す。"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_post  # noqa: E402


def _write(root: pathlib.Path, asin: str, name: str, nodes: list[str] | None = None) -> None:
    d = root / asin
    d.mkdir(parents=True, exist_ok=True)
    item = {"asin": asin, "title": name}
    if nodes is not None:
        item["browse_nodes"] = [{"name": n} for n in nodes]
    (d / "amazon.json").write_text(json.dumps({"asin": asin, "item": item}, ensure_ascii=False),
                                   encoding="utf-8")


def _comp(asin: str, name: str) -> dict:
    return {"asin": asin, "name": name, "price": 1000, "image": "", "url": ""}


class CompetitorTypeFilterTest(unittest.TestCase):
    def _run(self, self_name, comps):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write(root, "B000SELF00", self_name)
            (root / "B000SELF00" / "competitors.json").write_text(
                json.dumps({"competitors": comps}, ensure_ascii=False), encoding="utf-8")
            data = {"product": {"asin": "B000SELF00", "name": self_name,
                                "prices": {"amazon": {"price": 1000}}}}
            build_post._override_competitive_analysis(data, root)
            return [c["asin"] for c in data["competitive_analysis"]]

    def test_drops_other_type_and_backfills(self):
        got = self._run("ボードゲーム すごろく", [
            _comp("B000MINI40", "ミニ四駆 サーキット"),
            _comp("B000GAME01", "カードゲーム 神経衰弱"),
            _comp("B000TOMI01", "トミカ ミニカー"),
            _comp("B000GAME02", "ボードゲーム 対戦"),
        ])
        self.assertNotIn("B000MINI40", got)
        self.assertNotIn("B000TOMI01", got)
        self.assertEqual(got, ["B000GAME01", "B000GAME02"])

    def test_unknown_types_are_kept(self):
        got = self._run("ボードゲーム すごろく", [
            _comp("B000UNKN01", "なにかの商品 XYZ"),
        ])
        self.assertEqual(got, ["B000UNKN01"])

    def test_self_unknown_keeps_order(self):
        got = self._run("なにかの商品 XYZ", [
            _comp("B000MINI40", "ミニ四駆 サーキット"),
            _comp("B000GAME01", "カードゲーム"),
        ])
        self.assertEqual(got, ["B000MINI40", "B000GAME01"])

    def test_unknown_below_top_n_is_not_used_to_backfill(self):
        got = self._run("ボードゲーム すごろく", [
            _comp("B000MINI40", "ミニ四駆 サーキット"),
            _comp("B000GAME01", "カードゲーム"),
            _comp("B000TOMI01", "トミカ ミニカー"),
            _comp("B000UNKN01", "なにかの商品 XYZ"),
            _comp("B000GAME02", "ボードゲーム 対戦"),
        ])
        self.assertEqual(got, ["B000GAME01", "B000GAME02"])


if __name__ == "__main__":
    unittest.main()
