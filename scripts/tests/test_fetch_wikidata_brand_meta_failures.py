"""fetch_wikidata_brand_meta.main の取得失敗の扱い (API 障害で既存データを空にしない)。"""
from __future__ import annotations

import json

import pytest

from scripts import fetch_wikidata_brand_meta as fw


@pytest.fixture
def env(tmp_path, monkeypatch):
    hub = tmp_path / "brand_hub.json"
    hub.write_text(json.dumps({"brands": {"A": {}, "B": {}}}), encoding="utf-8")
    out = tmp_path / "brand_wikidata.json"
    out.write_text(json.dumps({"brands": {"A": {"qid": "Q1"}, "B": {"qid": "Q2"}}}),
                   encoding="utf-8")
    monkeypatch.setattr(fw, "BRAND_HUB", hub)
    monkeypatch.setattr(fw, "OUT", out)
    monkeypatch.setattr(fw, "ROOT", tmp_path)
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    return out


def test_all_brands_failing_returns_nonzero_and_keeps_file(env, monkeypatch):
    before = env.read_text(encoding="utf-8")

    def boom(brand):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(fw, "build_record", boom)
    assert fw.main() == 1
    assert env.read_text(encoding="utf-8") == before


def test_failed_brand_keeps_prior_record(env, monkeypatch):
    def partial(brand):
        if brand == "A":
            raise RuntimeError("timeout")
        return {"qid": "Q22"}

    monkeypatch.setattr(fw, "build_record", partial)
    assert fw.main() == 0
    brands = json.loads(env.read_text(encoding="utf-8"))["brands"]
    assert brands == {"A": {"qid": "Q1"}, "B": {"qid": "Q22"}}


def test_no_match_is_still_dropped(env, monkeypatch):
    # 「一致なし」は正常な答えなので、前回の記録を残さない (従来どおり)
    monkeypatch.setattr(fw, "build_record", lambda brand: None if brand == "A" else {"qid": "Q2"})
    assert fw.main() == 0
    brands = json.loads(env.read_text(encoding="utf-8"))["brands"]
    assert brands == {"B": {"qid": "Q2"}}


def test_all_failing_counts_without_curated_skips(env, monkeypatch):
    # BRAND_OVERRIDES で None (API を呼ばない) のブランドが混ざっていても、全滅は全滅
    hub = json.loads(fw.BRAND_HUB.read_text(encoding="utf-8"))
    hub["brands"]["SKIPME"] = {}
    fw.BRAND_HUB.write_text(json.dumps(hub), encoding="utf-8")
    monkeypatch.setitem(fw.BRAND_OVERRIDES, "SKIPME", None)
    before = env.read_text(encoding="utf-8")

    def rec(brand):
        if brand == "SKIPME":
            return None
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(fw, "build_record", rec)
    assert fw.main() == 1
    assert env.read_text(encoding="utf-8") == before
