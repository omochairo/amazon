"""Unit tests for build_discounts.py (#3332 価格OFFバッジのサイト全面展開)。

Coverage:
1. savings_percentage >= min_pct の ASIN が採択される。
2. savings_percentage < min_pct の ASIN は除外される。
3. fetched_at が stale_days より古い ASIN は除外される。
4. ASIN キーは大文字で出力される (product_card.html の $asin が upper 済みのため)。
5. 壊れた json / 非 dict json はスキップされ、他の正常な ASIN の集計を妨げない。
6. per-asin ルートが空/存在しない場合でも items が空で正常終了する (graceful)。
7. write_output が hugo/data/discounts.json 相当のファイルを書き出す。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_discounts as bd  # noqa: E402


NOW = datetime(2026, 7, 18, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_asin(root: Path, asin: str, *, pct, price, fetched_at, item_override=None):
    d = root / asin
    d.mkdir(parents=True, exist_ok=True)
    item = {"savings_percentage": pct, "price": price}
    if item_override is not None:
        item = item_override
    payload = {"asin": asin, "fetched_at": fetched_at, "item": item}
    (d / "amazon.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_latest(path: Path, items: dict) -> Path:
    """price_watch/latest.json 相当のフィクスチャを書く。

    ``items`` は ``{asin: {"off": int, "p": int, "ts": iso}}`` の shape
    (fetch_price_watch.py の latest.json items と同一)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _iso(NOW),
        "source": "creators-api",
        "stats": {"requested": len(items), "found": len(items), "missing": 0, "history_appended": 0},
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestAggregateAcceptance:
    def test_savings_at_or_above_min_pct_is_accepted(self, tmp_path):
        _write_asin(
            tmp_path, "b0testabcd",
            pct=10, price=1000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 1
        assert "B0TESTABCD" in payload["items"]
        assert payload["items"]["B0TESTABCD"]["pct"] == 10
        assert payload["items"]["B0TESTABCD"]["price"] == 1000

    def test_savings_below_min_pct_is_excluded(self, tmp_path):
        _write_asin(
            tmp_path, "b0lowpct001",
            pct=9, price=1000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0
        assert payload["items"] == {}

    def test_zero_savings_is_excluded(self, tmp_path):
        _write_asin(
            tmp_path, "b0zeropct01",
            pct=0, price=1000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0


class TestStaleness:
    def test_fetched_at_within_stale_days_is_accepted(self, tmp_path):
        _write_asin(
            tmp_path, "b0freshasin",
            pct=20, price=2000, fetched_at=_iso(NOW - timedelta(days=21)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 1

    def test_fetched_at_older_than_stale_days_is_excluded(self, tmp_path):
        _write_asin(
            tmp_path, "b0staleasin",
            pct=20, price=2000, fetched_at=_iso(NOW - timedelta(days=22)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0

    def test_missing_fetched_at_is_excluded(self, tmp_path):
        d = tmp_path / "b0nofetchat"
        d.mkdir(parents=True, exist_ok=True)
        payload = {"asin": "b0nofetchat", "item": {"savings_percentage": 30, "price": 1000}}
        (d / "amazon.json").write_text(json.dumps(payload), encoding="utf-8")
        result = bd.aggregate(tmp_path, now=NOW)
        assert result["count"] == 0


class TestAsinKeyCasing:
    def test_asin_key_is_uppercased_in_output(self, tmp_path):
        _write_asin(
            tmp_path, "b0mixedcase",
            pct=15, price=500, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        assert list(payload["items"].keys()) == ["B0MIXEDCASE"]


class TestGracefulSkips:
    def test_broken_json_is_skipped_without_raising(self, tmp_path):
        broken = tmp_path / "B0BROKEN001"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "amazon.json").write_text("{not valid json", encoding="utf-8")

        _write_asin(
            tmp_path, "b0goodasin1",
            pct=25, price=1500, fetched_at=_iso(NOW - timedelta(days=1)),
        )

        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 1
        assert "B0GOODASIN1" in payload["items"]

    def test_non_dict_json_is_skipped(self, tmp_path):
        d = tmp_path / "B0LISTROOT1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "amazon.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0

    def test_missing_item_field_is_skipped(self, tmp_path):
        d = tmp_path / "B0NOITEM001"
        d.mkdir(parents=True, exist_ok=True)
        payload_in = {"asin": "B0NOITEM001", "fetched_at": _iso(NOW - timedelta(days=1))}
        (d / "amazon.json").write_text(json.dumps(payload_in), encoding="utf-8")

        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0

    def test_missing_asin_field_is_skipped(self, tmp_path):
        d = tmp_path / "B0NOASINFLD"
        d.mkdir(parents=True, exist_ok=True)
        payload_in = {
            "fetched_at": _iso(NOW - timedelta(days=1)),
            "item": {"savings_percentage": 50, "price": 100},
        }
        (d / "amazon.json").write_text(json.dumps(payload_in), encoding="utf-8")

        payload = bd.aggregate(tmp_path, now=NOW)
        assert payload["count"] == 0


class TestEmptyInput:
    def test_empty_per_asin_root_returns_empty_items(self, tmp_path):
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        payload = bd.aggregate(empty_root, now=NOW)
        assert payload["count"] == 0
        assert payload["items"] == {}

    def test_nonexistent_per_asin_root_returns_empty_items_gracefully(self, tmp_path):
        missing_root = tmp_path / "does_not_exist"
        payload = bd.aggregate(missing_root, now=NOW)
        assert payload["count"] == 0
        assert payload["items"] == {}

    def test_main_exits_zero_on_empty_input(self, tmp_path, capsys):
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        missing_latest = tmp_path / "does_not_exist" / "latest.json"
        out_path = tmp_path / "out" / "discounts.json"
        rc = bd.main([
            "--per-asin-root", str(empty_root),
            "--latest", str(missing_latest),
            "--out", str(out_path),
        ])
        assert rc == 0
        assert out_path.exists()
        written = json.loads(out_path.read_text(encoding="utf-8"))
        assert written["count"] == 0
        assert written["items"] == {}


class TestPriceWatchPriority:
    """#3332 follow-up: price_watch (日次) が per_asin (週次) より優先されること。"""

    def test_price_watch_off_is_preferred_over_per_asin(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0BOTHASIN",
            pct=50, price=5000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {
            "B0BOTHASIN": {"off": 20, "p": 1800, "ts": _iso(NOW - timedelta(days=1))},
        })
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 1
        item = payload["items"]["B0BOTHASIN"]
        assert item["pct"] == 20
        assert item["price"] == 1800
        assert item["src"] == "price_watch"

    def test_per_asin_fallback_covers_watch_uncovered_asin_only(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0WATCHEDAS",
            pct=50, price=5000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        _write_asin(
            per_asin_root, "B0FALLBACK1",
            pct=15, price=900, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {
            "B0WATCHEDAS": {"off": 20, "p": 1800, "ts": _iso(NOW - timedelta(days=1))},
        })
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 2
        assert payload["items"]["B0WATCHEDAS"]["src"] == "price_watch"
        assert payload["items"]["B0WATCHEDAS"]["pct"] == 20
        assert payload["items"]["B0FALLBACK1"]["src"] == "per_asin"
        assert payload["items"]["B0FALLBACK1"]["pct"] == 15

    def test_stale_price_watch_falls_back_to_per_asin(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0STALEWATC",
            pct=30, price=3000, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        # watch_stale_days のデフォルトは3日。4日前は stale とみなされる。
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {
            "B0STALEWATC": {"off": 20, "p": 1800, "ts": _iso(NOW - timedelta(days=4))},
        })
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 1
        item = payload["items"]["B0STALEWATC"]
        assert item["src"] == "per_asin"
        assert item["pct"] == 30

    def test_price_watch_below_min_pct_falls_back_to_per_asin(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0LOWWATCH1",
            pct=25, price=2500, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {
            "B0LOWWATCH1": {"off": 5, "p": 1900, "ts": _iso(NOW - timedelta(days=1))},
        })
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 1
        item = payload["items"]["B0LOWWATCH1"]
        assert item["src"] == "per_asin"
        assert item["pct"] == 25

    def test_missing_latest_file_is_graceful_and_uses_per_asin_only(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0NOLATEST1",
            pct=15, price=1500, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        missing_latest = tmp_path / "does_not_exist" / "latest.json"
        payload = bd.aggregate(per_asin_root, latest_path=missing_latest, now=NOW)
        assert payload["count"] == 1
        assert payload["items"]["B0NOLATEST1"]["src"] == "per_asin"

    def test_empty_latest_items_is_graceful(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0EMPTYWATC",
            pct=15, price=1500, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {})
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 1
        assert payload["items"]["B0EMPTYWATC"]["src"] == "per_asin"

    def test_price_watch_only_asin_not_in_per_asin_is_accepted(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        per_asin_root.mkdir(parents=True, exist_ok=True)
        latest_path = _write_latest(tmp_path / "price_watch" / "latest.json", {
            "B0WATCHONLY1": {"off": 33, "p": 700, "ts": _iso(NOW - timedelta(days=1))},
        })
        payload = bd.aggregate(per_asin_root, latest_path=latest_path, now=NOW)
        assert payload["count"] == 1
        assert payload["items"]["B0WATCHONLY1"]["pct"] == 33
        assert payload["items"]["B0WATCHONLY1"]["src"] == "price_watch"

    def test_params_include_watch_stale_days(self, tmp_path):
        per_asin_root = tmp_path / "per_asin"
        per_asin_root.mkdir(parents=True, exist_ok=True)
        payload = bd.aggregate(per_asin_root, now=NOW)
        assert payload["params"] == {"min_pct": 10, "stale_days": 21, "watch_stale_days": 3}

    def test_omitting_latest_path_skips_price_watch_entirely(self, tmp_path):
        """latest_path 未指定時は従来通り per_asin のみで動く (呼び出し互換)。"""
        per_asin_root = tmp_path / "per_asin"
        _write_asin(
            per_asin_root, "B0LEGACYCAL",
            pct=15, price=1500, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(per_asin_root, now=NOW)
        assert payload["count"] == 1
        assert payload["items"]["B0LEGACYCAL"]["src"] == "per_asin"


class TestWriteOutput:
    def test_write_output_produces_expected_shape(self, tmp_path):
        _write_asin(
            tmp_path, "b0writeout1",
            pct=33, price=999, fetched_at=_iso(NOW - timedelta(days=1)),
        )
        payload = bd.aggregate(tmp_path, now=NOW)
        out_path = tmp_path / "out" / "discounts.json"
        bd.write_output(payload, out_path)

        assert out_path.exists()
        written = json.loads(out_path.read_text(encoding="utf-8"))
        assert written["params"] == {"min_pct": 10, "stale_days": 21, "watch_stale_days": 3}
        assert written["count"] == 1
        assert written["items"]["B0WRITEOUT1"]["pct"] == 33
        assert "generated_at" in written
