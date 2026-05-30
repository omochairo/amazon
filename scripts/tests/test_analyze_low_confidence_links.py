"""Tests for scripts/analyze_low_confidence_links.py (Issue #806)."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_low_confidence_links import (  # noqa: E402
    build_asin_to_jan,
    collect_low_confidence,
    render_markdown,
    split_by_jan,
)


def _write(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_collect_skips_verified_and_missing_url(tmp_path: pathlib.Path) -> None:
    articles = tmp_path / "articles"
    _write(articles / "A.json", {
        "slug": "A",
        "title": "T-A",
        "product": {
            "asin": "B001",
            "prices": {
                "rakuten": {"url": "https://r/", "price": 100, "verified": False},
                "yahoo": {"url": "https://y/", "price": 200, "verified": True},
            },
        },
    })
    _write(articles / "B.json", {
        "slug": "B",
        "title": "T-B",
        "product": {
            "asin": "B002",
            "prices": {
                "rakuten": {"url": "", "price": 0, "verified": False},
                "yahoo": {"url": "https://y2/", "price": 300, "verified": False, "is_search": True},
            },
        },
    })
    rows = collect_low_confidence(articles, asin_to_jan={"B001": "4900000000001"})
    asins_sites = sorted((r["asin"], r["site"]) for r in rows)
    assert asins_sites == [("B001", "rakuten"), ("B002", "yahoo")]
    rakuten_row = next(r for r in rows if r["asin"] == "B001")
    assert rakuten_row["jan"] == "4900000000001"
    yahoo_row = next(r for r in rows if r["asin"] == "B002")
    assert yahoo_row["is_search"] is True
    assert yahoo_row["jan"] is None


def test_split_dedups_unknown_when_other_row_has_jan(tmp_path: pathlib.Path) -> None:
    rows = [
        {"asin": "B001", "jan": "4900000000001", "site": "rakuten"},
        {"asin": "B001", "jan": None, "site": "yahoo"},  # 同 ASIN は known 側に寄せる
        {"asin": "B002", "jan": None, "site": "rakuten"},
    ]
    split = split_by_jan(rows)  # type: ignore[arg-type]
    assert split["jan_known"] == [{"asin": "B001", "jan": "4900000000001"}]
    assert split["jan_unknown"] == ["B002"]


def test_build_asin_to_jan_falls_back_to_per_asin(tmp_path: pathlib.Path) -> None:
    amazon = tmp_path / "amazon.json"
    _write(amazon, {"items": [{"asin": "B001", "jan_code": "4900000000001"}]})
    per_asin = tmp_path / "per_asin"
    _write(per_asin / "B002" / "amazon.json", {"item": {"jan_code": "4900000000002"}})
    # B003 has snapshot but no jan_code
    _write(per_asin / "B003" / "amazon.json", {"item": {"asin": "B003"}})
    mapping = build_asin_to_jan(amazon, per_asin, ["B001", "B002", "B003", "B004"])
    assert mapping == {"B001": "4900000000001", "B002": "4900000000002"}


def test_render_markdown_includes_header_and_total() -> None:
    rows = [{
        "slug": "S", "title": "T", "asin": "B001", "jan": "4900000000001",
        "site": "rakuten", "site_label": "楽天市場",
        "price": 1234, "url": "https://x/", "is_search": False,
    }]
    md = render_markdown(rows)
    assert "Total: 1 links found." in md
    assert "`4900000000001`" in md
    assert "[1234円](https://x/)" in md
