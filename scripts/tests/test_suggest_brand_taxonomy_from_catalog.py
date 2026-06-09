"""scripts/suggest_brand_taxonomy_from_catalog.py unit tests (P3, epic #2126)."""
from __future__ import annotations

import json

from scripts.suggest_brand_taxonomy_from_catalog import (
    NON_BRAND_RAW_FOLDED,
    collect_candidates,
    finalize,
    run,
)


def _write_article(tmp_path, name, brand, asin="B000", title="t"):
    p = tmp_path / name
    p.write_text(
        json.dumps({"product": {"brand": brand, "asin": asin, "title": title}}),
        encoding="utf-8",
    )
    return str(p)


def test_known_brand_excluded(tmp_path):
    # レゴ is in brand_taxonomy → match_type != unknown → not a candidate
    f = _write_article(tmp_path, "a.json", "レゴ")
    collected = collect_candidates([f])
    assert collected["total_unknown_products"] == 0
    assert collected["groups"] == {}


def test_unknown_brand_collected(tmp_path):
    files = [
        _write_article(tmp_path, "a.json", "PicassoFoo", asin="B001"),
        _write_article(tmp_path, "b.json", "PicassoFoo", asin="B002"),
    ]
    collected = collect_candidates(files)
    assert collected["total_unknown_products"] == 2
    assert len(collected["groups"]) == 1


def test_empty_brand_skipped(tmp_path):
    f = _write_article(tmp_path, "a.json", "")
    collected = collect_candidates([f])
    assert collected["total_unknown_products"] == 0


def test_non_brand_raw_excluded(tmp_path):
    f = _write_article(tmp_path, "a.json", "ノーブランド")
    collected = collect_candidates([f])
    assert collected["total_unknown_products"] == 0


def test_non_brand_raw_folded_set_nonempty():
    assert NON_BRAND_RAW_FOLDED  # guard against fold returning all-empty


def test_fold_merges_case_and_width_variants(tmp_path):
    files = [
        _write_article(tmp_path, "a.json", "MysteryToy", asin="B001"),
        _write_article(tmp_path, "b.json", "mysterytoy", asin="B002"),
        _write_article(tmp_path, "c.json", "MYSTERY-TOY", asin="B003"),
    ]
    collected = collect_candidates(files)
    # all three fold to the same key
    assert len(collected["groups"]) == 1
    cands = finalize(collected["groups"], min_products=2, top_n=10)
    assert len(cands) == 1
    assert cands[0]["product_count"] == 3
    assert set(cands[0]["raw_variants"]) == {"MysteryToy", "mysterytoy", "MYSTERY-TOY"}


def test_min_products_threshold(tmp_path):
    files = [
        _write_article(tmp_path, "a.json", "SoloBrand", asin="B001"),
        _write_article(tmp_path, "b.json", "PairBrand", asin="B002"),
        _write_article(tmp_path, "c.json", "PairBrand", asin="B003"),
    ]
    collected = collect_candidates(files)
    cands = finalize(collected["groups"], min_products=2, top_n=10)
    displays = {c["display"] for c in cands}
    assert "PairBrand" in displays
    assert "SoloBrand" not in displays


def test_display_picks_most_frequent_variant(tmp_path):
    files = [
        _write_article(tmp_path, "a.json", "BrandX", asin="B001"),
        _write_article(tmp_path, "b.json", "BrandX", asin="B002"),
        _write_article(tmp_path, "c.json", "brandx", asin="B003"),
    ]
    collected = collect_candidates(files)
    cands = finalize(collected["groups"], min_products=2, top_n=10)
    assert cands[0]["display"] == "BrandX"


def test_run_sorts_by_product_count(tmp_path):
    files = [
        _write_article(tmp_path, "a.json", "Big", asin="B001"),
        _write_article(tmp_path, "b.json", "Big", asin="B002"),
        _write_article(tmp_path, "c.json", "Big", asin="B003"),
        _write_article(tmp_path, "d.json", "Small", asin="B004"),
        _write_article(tmp_path, "e.json", "Small", asin="B005"),
    ]
    result = run(str(tmp_path / "*.json"), min_products=2, top_n=10)
    assert result["candidates"][0]["display"] == "Big"
    assert result["candidates"][1]["display"] == "Small"
    assert result["total_unknown_products"] == 5


def test_sidecar_files_skipped(tmp_path):
    _write_article(tmp_path, "a.json", "RealBrandZ", asin="B001")
    _write_article(tmp_path, "a.quality.json", "RealBrandZ", asin="B001")
    _write_article(tmp_path, "a.seo.json", "RealBrandZ", asin="B001")
    from scripts.suggest_brand_taxonomy_from_catalog import iter_article_files
    files = iter_article_files(str(tmp_path / "*.json"))
    assert len(files) == 1
    assert files[0].endswith("a.json")
