"""Tests for scripts/backfill_jan_codes.py (Issue #806 Phase 2-2)."""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backfill_jan_codes import (  # noqa: E402
    extract_jan_from_item,
    fetch_jans,
    update_amazon_raw,
    update_per_asin,
)


def _ean_item(asin: str, ean: str) -> dict:
    return {
        "asin": asin,
        "itemInfo": {"externalIds": {"eans": {"displayValues": [ean]}}},
    }


def _isbn_item(asin: str, isbn: str) -> dict:
    return {
        "asin": asin,
        "itemInfo": {"externalIds": {"isbns": {"displayValues": [isbn]}}},
    }


def test_extract_jan_prefers_eans_over_isbns() -> None:
    item = {
        "asin": "B001",
        "itemInfo": {"externalIds": {
            "eans": {"displayValues": ["4900000000001"]},
            "isbns": {"displayValues": ["9784900000001"]},
        }},
    }
    assert extract_jan_from_item(item) == "4900000000001"


def test_extract_jan_falls_back_to_isbns() -> None:
    assert extract_jan_from_item(_isbn_item("B001", "9784900000001")) == "9784900000001"


def test_extract_jan_returns_empty_when_no_ids() -> None:
    assert extract_jan_from_item({"asin": "B001", "itemInfo": {}}) == ""
    assert extract_jan_from_item({}) == ""


class _FakeClient:
    def __init__(self, items_by_asin: dict[str, dict]) -> None:
        self._items = items_by_asin
        self.calls: list[list[str]] = []

    def get_items(self, asins: list[str], resources: list[str] | None = None) -> dict:
        self.calls.append(list(asins))
        items = [self._items[a] for a in asins if a in self._items]
        return {"itemsResult": {"items": items}}


def test_fetch_jans_batches_and_skips_missing() -> None:
    items = {
        "B001": _ean_item("B001", "4900000000001"),
        # B002 intentionally absent from API response
        "B003": _isbn_item("B003", "9784900000003"),
    }
    client = _FakeClient(items)
    result = fetch_jans(client, ["B001", "B002", "B003"])
    assert result == {"B001": "4900000000001", "B003": "9784900000003"}
    assert client.calls == [["B001", "B002", "B003"]]


def test_update_per_asin_writes_only_when_missing(tmp_path: pathlib.Path) -> None:
    per_asin = tmp_path / "per_asin"
    (per_asin / "B001").mkdir(parents=True)
    (per_asin / "B001" / "amazon.json").write_text(
        json.dumps({"item": {"asin": "B001"}}), encoding="utf-8"
    )
    (per_asin / "B002").mkdir(parents=True)
    (per_asin / "B002" / "amazon.json").write_text(
        json.dumps({"item": {"asin": "B002", "jan_code": "4900000000002"}}), encoding="utf-8"
    )

    assert update_per_asin(per_asin, "B001", "4900000000001") is True
    assert update_per_asin(per_asin, "B002", "OVERWRITE") is False  # already has jan
    assert update_per_asin(per_asin, "B999", "4900000000999") is False  # no snapshot

    snap1 = json.loads((per_asin / "B001" / "amazon.json").read_text(encoding="utf-8"))
    assert snap1["item"]["jan_code"] == "4900000000001"
    snap2 = json.loads((per_asin / "B002" / "amazon.json").read_text(encoding="utf-8"))
    assert snap2["item"]["jan_code"] == "4900000000002"  # unchanged


def test_update_amazon_raw_only_fills_blank(tmp_path: pathlib.Path) -> None:
    raw = tmp_path / "amazon.json"
    raw.write_text(json.dumps({"items": [
        {"asin": "B001"},
        {"asin": "B002", "jan_code": "EXISTING"},
        {"asin": "B003"},
    ]}), encoding="utf-8")
    updated = update_amazon_raw(raw, {"B001": "4900000000001", "B002": "SHOULD_NOT", "B004": "X"})
    assert updated == 1
    data = json.loads(raw.read_text(encoding="utf-8"))
    asin_to_jan = {it["asin"]: it.get("jan_code") for it in data["items"]}
    assert asin_to_jan == {"B001": "4900000000001", "B002": "EXISTING", "B003": None}
