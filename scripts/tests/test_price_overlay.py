"""Unit tests for price_overlay.py (#4007 価格の日次化 — 現在価格解決の単一 source)。

Coverage:
1. price_watch と per_asin の両方に観測があるとき price_watch (日次) が勝つ。
2. price_watch が stale なら per_asin (週次) にフォールバックする。
3. 両方 stale / 欠落なら resolve が None (呼び出し側は記事 JSON のまま fail-soft)。
4. ts / fetched_at が壊れている観測は採択しない (unknown を pass に潰さない)。
5. ``off`` キーが無い entry も価格として採択され、savings_percentage は None。
6. per_asin の旧形式 (item が root 直下) も読める。
7. savings_percentage=0 が None に潰れず 0 のまま保持される (割引なしの有効値)。
8. bool (``True``) が price / off として採択されない (bool は int のサブクラス)。
9. apply_to_amazon_entry が既存の凍結価格を上書きし、None のフィールドでは
   既存値を消さず、price_source / price_observed_at を残す。
10. 壊れた latest.json / 存在しないパスで load_watch_index が空 dict を返す。
11. ASIN キーが小文字入力でも大文字に正規化される。
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

import price_overlay as po  # noqa: E402


NOW = datetime(2026, 7, 26, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_latest(path: Path, items: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": _iso(NOW), "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_per_asin(root: Path, asin: str, *, item=None, fetched_at=None, flat=False) -> Path:
    d = root / asin
    d.mkdir(parents=True, exist_ok=True)
    body = dict(item or {})
    if flat:
        # 旧形式: item 相当が root 直下 (fetched_at と同階層)。
        payload = {"asin": asin, "fetched_at": fetched_at, **body}
    else:
        payload = {"asin": asin, "fetched_at": fetched_at, "item": body}
    p = d / "amazon.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1 / 2 / 3: 優先順とフォールバック
# ---------------------------------------------------------------------------

def test_price_watch_wins_over_per_asin(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 7927, "off": 26, "ts": _iso(NOW - timedelta(hours=12)),
                  "avail": "在庫あり。"}},
    )
    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B001", item={"price": 3149, "savings_percentage": 0},
                    fetched_at=_iso(NOW - timedelta(days=2)))

    idx = po.load_watch_index(latest, now=NOW)
    obs = po.resolve("B001", watch_index=idx, per_asin_root=per_asin, now=NOW)

    assert obs is not None
    assert obs.source == "price_watch"
    assert obs.price == 7927
    assert obs.savings_percentage == 26
    assert obs.availability == "在庫あり。"


def test_falls_back_to_per_asin_when_watch_is_stale(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 7927, "off": 26, "ts": _iso(NOW - timedelta(days=10))}},
    )
    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B001", item={"price": 3149, "savings_percentage": 5},
                    fetched_at=_iso(NOW - timedelta(days=2)))

    idx = po.load_watch_index(latest, now=NOW)
    assert idx == {}  # stale なので index に入らない

    obs = po.resolve("B001", watch_index=idx, per_asin_root=per_asin, now=NOW)
    assert obs is not None
    assert obs.source == "per_asin"
    assert obs.price == 3149
    assert obs.savings_percentage == 5


def test_resolve_returns_none_when_both_lanes_stale(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 7927, "ts": _iso(NOW - timedelta(days=10))}},
    )
    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B001", item={"price": 3149},
                    fetched_at=_iso(NOW - timedelta(days=60)))

    idx = po.load_watch_index(latest, now=NOW)
    assert po.resolve("B001", watch_index=idx, per_asin_root=per_asin, now=NOW) is None


def test_resolve_handles_missing_asin_and_empty_index(tmp_path):
    per_asin = tmp_path / "per_asin"
    per_asin.mkdir(parents=True, exist_ok=True)
    assert po.resolve("B404", watch_index={}, per_asin_root=per_asin, now=NOW) is None
    assert po.resolve("", watch_index=None, per_asin_root=per_asin, now=NOW) is None


# ---------------------------------------------------------------------------
# 4: unknown を pass に潰さない
# ---------------------------------------------------------------------------

def test_unparsable_timestamps_are_rejected_not_treated_as_fresh(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {
            "B001": {"p": 1000, "ts": "not-a-date"},
            "B002": {"p": 1000},                      # ts 欠落
            "B003": {"p": 1000, "ts": 20260726},      # 非 str
        },
    )
    assert po.load_watch_index(latest, now=NOW) == {}

    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B004", item={"price": 1000}, fetched_at="???")
    _write_per_asin(per_asin, "B005", item={"price": 1000}, fetched_at=None)
    assert po.load_per_asin_observation(per_asin, "B004", now=NOW) is None
    assert po.load_per_asin_observation(per_asin, "B005", now=NOW) is None


# ---------------------------------------------------------------------------
# 5 / 7: off / savings_percentage の扱い
# ---------------------------------------------------------------------------

def test_entry_without_off_key_is_still_adopted_with_none_savings(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "ts": _iso(NOW)}},
    )
    idx = po.load_watch_index(latest, now=NOW)
    assert idx["B001"].price == 3149
    assert idx["B001"].savings_percentage is None
    assert idx["B001"].availability is None


def test_zero_savings_percentage_is_preserved_not_collapsed_to_none(tmp_path):
    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B001", item={"price": 3149, "savings_percentage": 0},
                    fetched_at=_iso(NOW))
    obs = po.load_per_asin_observation(per_asin, "B001", now=NOW)
    assert obs is not None
    assert obs.savings_percentage == 0


def test_out_of_range_off_is_unknown(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 3149, "off": 140, "ts": _iso(NOW)},
         "B002": {"p": 3149, "off": -5, "ts": _iso(NOW)}},
    )
    idx = po.load_watch_index(latest, now=NOW)
    assert idx["B001"].savings_percentage is None
    assert idx["B002"].savings_percentage is None


# ---------------------------------------------------------------------------
# 6: per_asin 旧形式
# ---------------------------------------------------------------------------

def test_legacy_flat_per_asin_snapshot_is_readable(tmp_path):
    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B001",
                    item={"price": 2480, "savings_percentage": 12,
                          "availability": "在庫あり。"},
                    fetched_at=_iso(NOW - timedelta(days=1)), flat=True)
    obs = po.load_per_asin_observation(per_asin, "B001", now=NOW)
    assert obs is not None
    assert obs.price == 2480
    assert obs.savings_percentage == 12
    assert obs.availability == "在庫あり。"


# ---------------------------------------------------------------------------
# 8: bool 除け
# ---------------------------------------------------------------------------

def test_bool_is_not_adopted_as_price_or_off(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": True, "ts": _iso(NOW)},
         "B002": {"p": 3149, "off": True, "ts": _iso(NOW)}},
    )
    idx = po.load_watch_index(latest, now=NOW)
    assert "B001" not in idx           # True が 1 円として採択されない
    assert idx["B002"].savings_percentage is None

    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B003", item={"price": True}, fetched_at=_iso(NOW))
    assert po.load_per_asin_observation(per_asin, "B003", now=NOW) is None


def test_zero_or_negative_price_is_rejected(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"B001": {"p": 0, "ts": _iso(NOW)},
         "B002": {"p": -100, "ts": _iso(NOW)},
         "B003": {"p": "3149", "ts": _iso(NOW)}},
    )
    assert po.load_watch_index(latest, now=NOW) == {}


# ---------------------------------------------------------------------------
# 9: apply_to_amazon_entry
# ---------------------------------------------------------------------------

def test_apply_overwrites_frozen_price():
    amazon = {"price": 2518, "availability": "在庫あり。", "savings_percentage": 0,
              "url": "https://www.amazon.co.jp/dp/B001/?tag=x-22"}
    obs = po.PriceObservation(asin="B001", price=7927, savings_percentage=26,
                              availability="残り3点", observed_at="2026-07-25T21:11:27Z",
                              source="price_watch")
    changed = po.apply_to_amazon_entry(amazon, obs)

    assert changed is True
    assert amazon["price"] == 7927
    assert amazon["savings_percentage"] == 26
    assert amazon["availability"] == "残り3点"
    assert amazon["price_source"] == "price_watch"
    assert amazon["price_observed_at"] == "2026-07-25T21:11:27Z"
    assert amazon["url"].endswith("tag=x-22")  # 無関係なキーは触らない


def test_apply_with_none_obs_is_noop():
    amazon = {"price": 2518}
    assert po.apply_to_amazon_entry(amazon, None) is False
    assert amazon == {"price": 2518}


def test_apply_does_not_erase_existing_values_when_observation_unknown():
    amazon = {"price": 2518, "savings_percentage": 15, "availability": "在庫あり。"}
    obs = po.PriceObservation(asin="B001", price=3000, savings_percentage=None,
                              availability=None, observed_at="2026-07-25T21:11:27Z",
                              source="price_watch")
    po.apply_to_amazon_entry(amazon, obs)

    assert amazon["price"] == 3000
    assert amazon["savings_percentage"] == 15   # 消さない
    assert amazon["availability"] == "在庫あり。"  # 消さない


def test_apply_without_price_keeps_price_but_records_source():
    amazon = {"price": 2518}
    obs = po.PriceObservation(asin="B001", price=None, savings_percentage=30,
                              availability=None, observed_at="2026-07-25T21:11:27Z",
                              source="per_asin")
    changed = po.apply_to_amazon_entry(amazon, obs)

    assert changed is True
    assert amazon["price"] == 2518              # 価格は触らない
    assert amazon["savings_percentage"] == 30
    assert amazon["price_source"] == "per_asin"


def test_apply_returns_false_when_nothing_changes():
    obs = po.PriceObservation(asin="B001", price=2518, savings_percentage=None,
                              availability=None, observed_at="2026-07-25T21:11:27Z",
                              source="price_watch")
    amazon = {"price": 2518, "price_source": "price_watch",
              "price_observed_at": "2026-07-25T21:11:27Z"}
    assert po.apply_to_amazon_entry(amazon, obs) is False


# ---------------------------------------------------------------------------
# 10 / 11: graceful / 正規化
# ---------------------------------------------------------------------------

def test_load_watch_index_is_graceful_on_broken_or_missing_input(tmp_path):
    missing = tmp_path / "nope" / "latest.json"
    assert po.load_watch_index(missing, now=NOW) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert po.load_watch_index(broken, now=NOW) == {}

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"items": ["B001"]}), encoding="utf-8")
    assert po.load_watch_index(wrong_shape, now=NOW) == {}

    not_dict = tmp_path / "list.json"
    not_dict.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert po.load_watch_index(not_dict, now=NOW) == {}


def test_asin_keys_are_upper_normalized(tmp_path):
    latest = _write_latest(
        tmp_path / "price_watch" / "latest.json",
        {"b0875fv2bq": {"p": 3149, "ts": _iso(NOW)}},
    )
    idx = po.load_watch_index(latest, now=NOW)
    assert "B0875FV2BQ" in idx
    assert idx["B0875FV2BQ"].asin == "B0875FV2BQ"

    per_asin = tmp_path / "per_asin"
    _write_per_asin(per_asin, "B0875FV2BQ", item={"price": 2000}, fetched_at=_iso(NOW))
    obs = po.resolve("b0875fv2bq", watch_index={}, per_asin_root=per_asin, now=NOW)
    assert obs is not None and obs.asin == "B0875FV2BQ"
