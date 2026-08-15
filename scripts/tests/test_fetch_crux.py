"""scripts/fetch_crux.py unit tests (B-1, epic #1357).

ネットワーク呼出は call_crux をモックして避ける。
"""
from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import patch

import pytest

from scripts.fetch_crux import (
    CRUX_HISTORY_FILENAME,
    SEEN_DATES_FILENAME,
    append_records,
    crux_is_done,
    fetch_for_target,
    flatten_metrics,
    get_top_urls_from_gsc,
    load_seen,
    main,
    mark_crux_done,
    parse_origins,
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


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_get_top_urls_handles_missing_file(tmp_path):
    assert get_top_urls_from_gsc([tmp_path / "nope.jsonl"], "https://x.test", 3) == []


def test_get_top_urls_takes_top_impressions_of_latest_date(tmp_path):
    """履歴 JSONL は日付ぶん積まれているので、最新 date に絞ってから上位を採る。"""
    gsc = _write_jsonl(tmp_path / "gsc_by_page.jsonl", [
        # 古い日の行は、impressions が大きくても混ぜない
        {"date": "2026-08-01", "page": "https://navi.omcha.jp/old/", "impressions": 9999},
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/products/B0BBB/", "impressions": 100},
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/products/B0AAA/", "impressions": 500},
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/posts/foo/", "impressions": 200},
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/skip/", "impressions": 1},
    ])
    assert get_top_urls_from_gsc([gsc], "https://navi.omcha.jp", n=3) == [
        "https://navi.omcha.jp/products/B0AAA/",
        "https://navi.omcha.jp/posts/foo/",
        "https://navi.omcha.jp/products/B0BBB/",
    ]


def test_get_top_urls_splits_inputs_by_origin(tmp_path):
    """navi と WP の系列を両方渡しても、origin の行だけを拾う (#5080 項目3)。"""
    navi = _write_jsonl(tmp_path / "gsc_by_page.jsonl", [
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/a/", "impressions": 100},
    ])
    wp = _write_jsonl(tmp_path / "gsc_wp_by_page.jsonl", [
        {"date": "2026-08-08", "page": "https://omcha.jp/b/", "impressions": 900},
    ])
    assert get_top_urls_from_gsc([navi, wp], "https://navi.omcha.jp", n=3) == [
        "https://navi.omcha.jp/a/"]
    assert get_top_urls_from_gsc([navi, wp], "https://omcha.jp", n=3) == [
        "https://omcha.jp/b/"]


def test_get_top_urls_prefix_match_respects_boundary(tmp_path):
    """origin の前方一致が別ホストを巻き込まない。

    "https://omcha.jp" は "https://omcha.jpx.test" に一致してはいけないし、
    navi.omcha.jp は omcha.jp の subdomain だが別 origin なので混ざらない。
    """
    gsc = _write_jsonl(tmp_path / "g.jsonl", [
        {"date": "2026-08-08", "page": "https://omcha.jpx.test/a/", "impressions": 900},
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/b/", "impressions": 800},
        {"date": "2026-08-08", "page": "https://omcha.jp/c/", "impressions": 10},
    ])
    assert get_top_urls_from_gsc([gsc], "https://omcha.jp", n=5) == ["https://omcha.jp/c/"]


def test_get_top_urls_accepts_trailing_slash_origin(tmp_path):
    gsc = _write_jsonl(tmp_path / "g.jsonl", [
        {"date": "2026-08-08", "page": "https://x.test/a/", "impressions": 100},
    ])
    assert get_top_urls_from_gsc([gsc], "https://x.test/", n=3) == ["https://x.test/a/"]


def test_get_top_urls_skips_empty_page_and_dedupes(tmp_path):
    gsc = _write_jsonl(tmp_path / "g.jsonl", [
        {"date": "2026-08-08", "page": "", "impressions": 999},
        {"date": "2026-08-08", "page": "https://x.test/a/", "impressions": 100},
        {"date": "2026-08-08", "page": "https://x.test/a/", "impressions": 50},
    ])
    assert get_top_urls_from_gsc([gsc], "https://x.test", n=3) == ["https://x.test/a/"]


def test_get_top_urls_survives_malformed_input(tmp_path):
    """壊れた入力で origin 計測ごと落とさない (top URL は best-effort)。"""
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    good = _write_jsonl(tmp_path / "good.jsonl", [
        {"date": "2026-08-08", "page": "https://x.test/a/", "impressions": 100},
    ])
    assert get_top_urls_from_gsc([bad, good], "https://x.test", n=3) == ["https://x.test/a/"]


# ---------- 複数 origin (#5080 項目3) ----------

def test_parse_origins_accepts_comma_and_whitespace():
    assert parse_origins("https://a.test,https://b.test") == [
        "https://a.test", "https://b.test"]
    assert parse_origins("https://a.test https://b.test") == [
        "https://a.test", "https://b.test"]
    assert parse_origins(" https://a.test , https://b.test ") == [
        "https://a.test", "https://b.test"]


def test_parse_origins_single_value_still_works():
    """既存の単一値 secret はそのまま動く (後方互換)。"""
    assert parse_origins("https://omcha.jp") == ["https://omcha.jp"]


def test_parse_origins_strips_trailing_slash_and_dedupes():
    assert parse_origins("https://a.test/,https://a.test") == ["https://a.test"]


def test_parse_origins_empty():
    assert parse_origins(None) == []
    assert parse_origins("") == []
    assert parse_origins("  ,  ") == []


def test_crux_done_is_keyed_by_date_and_origin():
    """origin ごとに独立して済み判定する (date だけだと 2 つ目が丸ごと skip)。"""
    seen_crux = {}
    mark_crux_done(seen_crux, "2026-09-01", "https://a.test")
    assert crux_is_done(seen_crux, "2026-09-01", "https://a.test") is True
    assert crux_is_done(seen_crux, "2026-09-01", "https://b.test") is False
    assert crux_is_done(seen_crux, "2026-09-02", "https://a.test") is False


def test_crux_done_normalizes_trailing_slash():
    seen_crux = {}
    mark_crux_done(seen_crux, "2026-09-01", "https://a.test/")
    assert crux_is_done(seen_crux, "2026-09-01", "https://a.test") is True


def test_crux_done_treats_legacy_true_as_all_origins_done():
    """単一 origin 時代の `date: True` は全 origin 済みとみなす (二重 append を防ぐ)。"""
    seen_crux = {"2026-08-06": True}
    assert crux_is_done(seen_crux, "2026-08-06", "https://a.test") is True
    assert crux_is_done(seen_crux, "2026-08-06", "https://b.test") is True


def test_mark_crux_done_does_not_clobber_legacy_true():
    """旧形式を dict に潰すと、記録していない origin が未取得に見えて二重 append する。"""
    seen_crux = {"2026-08-06": True}
    mark_crux_done(seen_crux, "2026-08-06", "https://a.test")
    assert seen_crux["2026-08-06"] is True
    assert crux_is_done(seen_crux, "2026-08-06", "https://b.test") is True


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


# ---------- main(): 複数 origin の end-to-end (#5080 項目3) ----------

def _run_main(tmp_path, monkeypatch, argv_extra, seen=None):
    """call_crux をモックして main() を回し、(rc, 問い合わせた body, 履歴行) を返す。"""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    if seen is not None:
        (history_dir / SEEN_DATES_FILENAME).write_text(
            json.dumps(seen), encoding="utf-8")

    calls = []

    def fake_call(api_key, body, timeout=30):
        calls.append(dict(body))
        return {"record": _make_record()}

    argv = ["fetch_crux.py", "--api-key", "KEY",
            "--history-dir", str(history_dir),
            "--target-date", "2026-09-01",
            "--form-factors", "PHONE"] + argv_extra
    if "--gsc-input" not in argv:
        # 既定はリポジトリの実 GSC 履歴を指すので、明示しないテストでは
        # origin だけを見るよう存在しないパスに向ける
        argv += ["--gsc-input", str(tmp_path / "no-gsc.jsonl")]
    monkeypatch.setattr(sys, "argv", argv)
    with patch("scripts.fetch_crux.call_crux", side_effect=fake_call):
        rc = main()

    history_path = history_dir / CRUX_HISTORY_FILENAME
    rows = ([json.loads(l) for l in
             history_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if history_path.exists() else [])
    seen_path = history_dir / SEEN_DATES_FILENAME
    seen_after = (json.loads(seen_path.read_text(encoding="utf-8"))
                  if seen_path.exists() else {})
    return rc, calls, rows, seen_after


def test_main_fetches_every_origin_in_one_process(tmp_path, monkeypatch):
    """1 プロセスで両 origin を回す。date 単位の skip で 2 つ目が落ちない。"""
    monkeypatch.delenv("CRUX_ORIGIN", raising=False)
    rc, calls, rows, seen = _run_main(
        tmp_path, monkeypatch,
        ["--origin", "https://navi.omcha.jp", "https://omcha.jp"])

    assert rc == 0
    assert [c["origin"] for c in calls] == [
        "https://navi.omcha.jp", "https://omcha.jp"]
    assert [r["key_value"] for r in rows] == [
        "https://navi.omcha.jp", "https://omcha.jp"]
    # seen は (date, origin) で記録される
    assert seen["crux"]["2026-09-01"] == {
        "https://navi.omcha.jp": True, "https://omcha.jp": True}


def test_main_reads_multiple_origins_from_single_env_secret(tmp_path, monkeypatch):
    """単一 secret にカンマ区切りで入れても複数 origin として扱う。"""
    monkeypatch.setenv("CRUX_ORIGIN", "https://navi.omcha.jp,https://omcha.jp")
    rc, calls, rows, _ = _run_main(tmp_path, monkeypatch, [])
    assert rc == 0
    assert [c["origin"] for c in calls] == [
        "https://navi.omcha.jp", "https://omcha.jp"]


def test_main_single_env_origin_backward_compatible(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUX_ORIGIN", "https://omcha.jp")
    rc, calls, rows, _ = _run_main(tmp_path, monkeypatch, [])
    assert rc == 0
    assert [c["origin"] for c in calls] == ["https://omcha.jp"]


def test_main_skips_only_the_origin_already_fetched(tmp_path, monkeypatch):
    """片方だけ済んでいるとき、残りの origin は取りに行く。"""
    monkeypatch.delenv("CRUX_ORIGIN", raising=False)
    rc, calls, rows, seen = _run_main(
        tmp_path, monkeypatch,
        ["--origin", "https://navi.omcha.jp", "https://omcha.jp"],
        seen={"crux": {"2026-09-01": {"https://navi.omcha.jp": True}}})

    assert rc == 0
    assert [c["origin"] for c in calls] == ["https://omcha.jp"]
    assert seen["crux"]["2026-09-01"] == {
        "https://navi.omcha.jp": True, "https://omcha.jp": True}


def test_main_no_op_when_all_origins_done(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUX_ORIGIN", raising=False)
    rc, calls, rows, _ = _run_main(
        tmp_path, monkeypatch, ["--origin", "https://omcha.jp"],
        seen={"crux": {"2026-09-01": {"https://omcha.jp": True}}})
    assert rc == 0
    assert calls == []
    assert rows == []


def test_main_errors_when_no_origin_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUX_ORIGIN", raising=False)
    rc, calls, _, _ = _run_main(tmp_path, monkeypatch, [])
    assert rc == 2
    assert calls == []


def test_main_fetches_top_urls_per_origin(tmp_path, monkeypatch):
    """top URL は origin ごとに、その origin の GSC 行だけから選ばれる。"""
    monkeypatch.delenv("CRUX_ORIGIN", raising=False)
    navi = _write_jsonl(tmp_path / "navi.jsonl", [
        {"date": "2026-08-08", "page": "https://navi.omcha.jp/a/", "impressions": 100},
    ])
    wp = _write_jsonl(tmp_path / "wp.jsonl", [
        {"date": "2026-08-08", "page": "https://omcha.jp/b/", "impressions": 900},
    ])
    rc, calls, rows, _ = _run_main(
        tmp_path, monkeypatch,
        ["--origin", "https://navi.omcha.jp", "https://omcha.jp",
         "--gsc-input", str(navi), str(wp), "--top-urls", "1"])

    assert rc == 0
    assert calls == [
        {"origin": "https://navi.omcha.jp", "formFactor": "PHONE"},
        {"url": "https://navi.omcha.jp/a/", "formFactor": "PHONE"},
        {"origin": "https://omcha.jp", "formFactor": "PHONE"},
        {"url": "https://omcha.jp/b/", "formFactor": "PHONE"},
    ]
