"""Unit tests for fetch_amazon.extract_jan external-id fallback (Issue #2747).

extract_jan must mirror backfill_jan_codes.extract_jan_from_item: prefer eans,
then fall back to isbns and upcs, so books (ISBN13) and imports (UPC12) still
yield an identifier for JAN-keyed cross-search.
"""
from __future__ import annotations

import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import fetch_amazon as fa  # noqa: E402


def _item(**ids: str) -> dict:
    ext = {k: {"displayValues": [v]} for k, v in ids.items()}
    return {"itemInfo": {"externalIds": ext}}


def test_prefers_ean() -> None:
    assert fa.extract_jan(_item(eans="4900000000001", isbns="9784900000001")) == "4900000000001"


def test_falls_back_to_isbn() -> None:
    assert fa.extract_jan(_item(isbns="9784900000001")) == "9784900000001"


def test_falls_back_to_upc() -> None:
    assert fa.extract_jan(_item(upcs="012345678905")) == "012345678905"


def test_empty_when_no_external_ids() -> None:
    assert fa.extract_jan({"itemInfo": {}}) == ""
    assert fa.extract_jan({}) == ""


def test_skips_blank_values() -> None:
    # blank eans should fall through to isbns
    assert fa.extract_jan(_item(eans="  ", isbns="9784900000001")) == "9784900000001"
