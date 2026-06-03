"""scripts/fetch_crux.py unit tests (B-1, epic #1357).

ネットワーク呼出は call_crux をモックして避ける。
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from scripts.fetch_crux import (
    CRUX_HISTORY_FILENAME,
    SEEN_DATES_FILENAME,
    append_records,
    fetch_for_target,
    flatten_metrics,
    get_top_urls_from_gsc,
    load_seen,
    save_seen,
)


def _make_record(lcp_p75=2200, lcp_good=0.65, cls_p75=0.05):
    return {
        "key": {"origin": "https://example.test"},
        "metrics": {
            "largest_contentful_paint": {
                "histogram": [
                    {"start": 0, "end": 2500, "density": lcp_good},
                    {"start": 2500, "end": 4000, "density": 0.20},
                    {"start": 4000, "density": 0.15},
                ],
                "percentiles": {"p75": lcp_p75},
            },
            "cumulative_layout_shift": {
                "histogram": [
                    {"start": "0.00", "end": "0.10", "density": 0.85},
                    {"start": "0.10", "end": "0.25", "density": 0.10},
                    {"start": "0.25", "density": 0.05},
                ],
                "percentiles": {"p75": cls_p75},
            },
        },
        "collectionPeriod": {
            "firstDate": {"year": 2026, "month": 5, "day": 4},
            "lastDate":  {"year": 2026, "month": 6, "day": 1},
        },
    }


def test_flatten_metrics_full():
    flat = flatten_metrics(_make_record())
    assert flat["lcp_p75"] == 2200
    assert flat["lcp_good_density"] == 0.65
    assert flat["lcp_ni_density"] == 0.20
    assert flat["lcp_poor_density"] == 0.15
    assert flat["cls_p75"] == 0.05
    assert flat["cls_good_density"] == 0.85


def test_flatten_metrics_empty_record():
    assert flatten_metrics({}) == {}
    assert flatten_metrics({"metrics": {}}) == {}


def test_flatten_metrics_partial_metric_only_p75():
    rec = {"metrics": {"largest_contentful_paint": {"percentiles": {"p75": 1500}}}}
    flat = flatten_metrics(rec)
    assert flat == {"lcp_p75": 1500}


def test_fetch_for_target_aggregates_form_factors():
    target = {"origin": "https://example.test"}
    def fake_call(api_key, body, timeout=30):
        return {"record": _make_record(lcp_p75=2000 if body["formFactor"] == "PHONE" else 1500)}

    with patch("scripts.fetch_crux.call_crux", side_effect=fake_call):
        records = fetch_for_target("KEY", target, ("PHONE", "DESKTOP"))

    assert len(records) == 2
    assert records[0]["form_factor"] == "PHONE"
    assert records[0]["lcp_p75"] == 2000
    assert records[1]["form_factor"] == "DESKTOP"
    assert records[1]["lcp_p75"] == 1500
    for r in records:
        assert r["key_type"] == "origin"
        assert r["key_value"] == "https://example.test"
        assert r["collection_period_start"] == "2026-05-04"
        assert r["collection_period_end"] == "2026-06-01"


def test_fetch_for_target_404_skips_silently():
    with patch("scripts.fetch_crux.call_crux", return_value=None):
        records = fetch_for_target("KEY", {"origin": "https://no-data.test"}, ("PHONE",))
    assert records == []


def test_fetch_for_target_exception_continues_with_other_form_factors():
    def fake_call(api_key, body, timeout=30):
        if body["formFactor"] == "PHONE":
            raise RuntimeError("net error")
        return {"record": _make_record(lcp_p75=1500)}

    with patch("scripts.fetch_crux.call_crux", side_effect=fake_call):
        records = fetch_for_target("KEY", {"origin": "https://x.test"}, ("PHONE", "DESKTOP"))
    # PHONE は例外で skip、DESKTOP は成功
    assert len(records) == 1
    assert records[0]["form_factor"] == "DESKTOP"


def test_get_top_urls_handles_missing_file(tmp_path):
    assert get_top_urls_from_gsc(tmp_path / "nope.json", "https://x.test", 3) == []


def test_get_top_urls_prefixes_origin_for_relative_paths(tmp_path):
    gsc = tmp_path / "gsc_weekly.json"
    gsc.write_text(json.dumps({
        "by_page": [
            {"page": "/products/B0AAA/", "impressions": 500},
            {"page": "https://navi.omcha.jp/posts/foo/", "impressions": 200},
            {"page": "/products/B0BBB/", "impressions": 100},
            {"page": "/skip/", "impressions": 50},
        ]
    }), encoding="utf-8")
    urls = get_top_urls_from_gsc(gsc, "https://navi.omcha.jp", n=3)
    assert urls == [
        "https://navi.omcha.jp/products/B0AAA/",
        "https://navi.omcha.jp/posts/foo/",
        "https://navi.omcha.jp/products/B0BBB/",
    ]


def test_get_top_urls_skips_empty_page(tmp_path):
    gsc = tmp_path / "gsc_weekly.json"
    gsc.write_text(json.dumps({
        "by_page": [
            {"page": "", "impressions": 999},
            {"page": "/a/", "impressions": 100},
        ]
    }), encoding="utf-8")
    urls = get_top_urls_from_gsc(gsc, "https://x.test", n=3)
    assert urls == ["https://x.test/a/"]


def test_append_records_creates_jsonl_with_date_column(tmp_path):
    path = tmp_path / "crux.jsonl"
    records = [{
        "key_type": "origin", "key_value": "https://x.test",
        "form_factor": "PHONE", "lcp_p75": 2000,
    }]
    n = append_records(path, "2026-06-02", records)
    assert n == 1
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["date"] == "2026-06-02"
    assert line["lcp_p75"] == 2000


def test_append_records_empty_noop(tmp_path):
    path = tmp_path / "crux.jsonl"
    assert append_records(path, "2026-06-02", []) == 0
    assert not path.exists()


def test_seen_dates_roundtrip(tmp_path):
    seen_path = tmp_path / SEEN_DATES_FILENAME
    save_seen(seen_path, {"crux": {"2026-06-02": True}})
    loaded = load_seen(seen_path)
    assert loaded["crux"]["2026-06-02"] is True


def test_load_seen_missing_returns_empty(tmp_path):
    assert load_seen(tmp_path / "nope.json") == {}
