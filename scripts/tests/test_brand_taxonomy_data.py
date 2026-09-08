"""data/brand_taxonomy.yaml と data/brand_rejected.yaml のデータ整合性テスト (#2931)。"""
from __future__ import annotations

import pathlib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TAXONOMY_PATH = REPO_ROOT / "data" / "brand_taxonomy.yaml"
REJECTED_PATH = REPO_ROOT / "data" / "brand_rejected.yaml"

VALID_TIERS = {"S", "A", "B", "C", "D"}


def _brands() -> list[dict]:
    data = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    return data["brands"]


def _rejected() -> list[dict]:
    data = yaml.safe_load(REJECTED_PATH.read_text(encoding="utf-8"))
    return data["rejected"]


def test_no_duplicate_canonical():
    canonicals = [b["canonical"] for b in _brands()]
    dups = {c for c in canonicals if canonicals.count(c) > 1}
    assert not dups, f"duplicate canonical entries: {dups}"


def test_noindex_entries_satisfy_schema():
    for b in _brands():
        if not b.get("noindex"):
            continue
        assert isinstance(b["canonical"], str) and b["canonical"]
        assert b.get("tier") in VALID_TIERS, b
        assert isinstance(b.get("region"), str) and b["region"], b
        assert isinstance(b.get("aliases"), list) and b["aliases"], b


def test_no_duplicate_rejected_brand():
    brands = [r["brand"] for r in _rejected()]
    dups = {b for b in brands if brands.count(b) > 1}
    assert not dups, f"duplicate rejected brand entries: {dups}"


def test_rejected_and_taxonomy_do_not_overlap():
    taxonomy_canonicals = {b["canonical"] for b in _brands()}
    rejected_brands = {r["brand"] for r in _rejected()}
    overlap = taxonomy_canonicals & rejected_brands
    assert not overlap, f"brands present in both taxonomy and rejected list: {overlap}"
