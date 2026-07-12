"""test_build_asin_query_context.py — #2953 B案 (#1329 の記事版)

build_asin_query_context.py の純関数 / build_context を、tmp_path に合成した
GSC 週次スナップショット (data/analytics/gsc_history/<YYYY-Www>.json 相当) で検証する。
実データには依存しない。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from build_asin_query_context import (  # noqa: E402
    build_context,
    normalize,
    normalize_page_path,
    _asin_page_match,
)

ASIN = "B0FNM4Y35G"
PAGE = f"https://navi.omcha.jp/products/{ASIN.lower()}/"


def _combo(query, page, impressions, clicks=0, position=5.0):
    return {"query": query, "page": page, "impressions": impressions,
            "clicks": clicks, "position": position}


def _write_snapshot(dir_path: pathlib.Path, week_label: str, by_combo=None, raw_text=None):
    p = dir_path / f"{week_label}.json"
    if raw_text is not None:
        p.write_text(raw_text, encoding="utf-8")
        return p
    payload = {"by_combo": by_combo or []}
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_normalize_nfkc_lower_bracket():
    assert normalize("ベイブレードＸ") == "ベイブレードx"
    assert normalize("わくわく ()") == "わくわく"


def test_normalize_page_path_decode_lower():
    assert (normalize_page_path(f"https://navi.omcha.jp/products/{ASIN}/")
            == f"/products/{ASIN.lower()}/")


def test_asin_page_match_exact_only():
    assert _asin_page_match(f"/products/{ASIN.lower()}/", ASIN.lower())
    assert not _asin_page_match(f"/products/{ASIN.lower()}/reviews/", ASIN.lower())
    assert not _asin_page_match("/products/other0000/", ASIN.lower())


def test_aggregates_across_weeks_and_weighted_position(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    _write_snapshot(hist, "2026-W23", by_combo=[
        _combo("ジェリーブロックス 口コミ", PAGE, impressions=10, clicks=1, position=4.0),
    ])
    _write_snapshot(hist, "2026-W24", by_combo=[
        _combo("ジェリーブロックス 口コミ", PAGE, impressions=20, clicks=3, position=7.0),
    ])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is False
    assert r["weeks_scanned"] == 2
    assert len(r["queries"]) == 1
    q = r["queries"][0]
    assert q["query"] == "ジェリーブロックス 口コミ"
    assert q["impressions"] == 30
    assert q["clicks"] == 4
    assert q["weeks_seen"] == 2
    # weighted avg position = (4*10 + 7*20) / 30 = 6.0
    assert q["position"] == 6.0


def test_representative_query_from_max_impression_week(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    # 大文字小文字違い + 全角/半角違いだが normalize() では同一キーになるペア
    _write_snapshot(hist, "2026-W23", by_combo=[
        _combo("jelly blocks", PAGE, impressions=2, clicks=0, position=8.0),
    ])
    _write_snapshot(hist, "2026-W24", by_combo=[
        _combo("JELLY BLOCKS", PAGE, impressions=9, clicks=1, position=3.0),
    ])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is False
    # 正規化キーは同一だが代表表記は impressions が多い週の生表記
    assert r["queries"][0]["query"] == "JELLY BLOCKS"
    assert r["queries"][0]["impressions"] == 11


def test_uppercase_asin_input_matches_lowercase_page_path(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    _write_snapshot(hist, "2026-W23", by_combo=[
        _combo("test query", PAGE, impressions=5, clicks=0, position=2.0),
    ])
    r = build_context(ASIN.lower(), history_dir=str(hist), weeks=4)
    assert r["fallback"] is False
    assert r["asin"] == ASIN  # 出力はキャノニカル (upper)


def test_max_queries_cap_and_sort_desc(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    rows = [_combo(f"query {i}", PAGE, impressions=i) for i in range(1, 13)]
    _write_snapshot(hist, "2026-W23", by_combo=rows)
    r = build_context(ASIN, history_dir=str(hist), weeks=4, max_queries=3, min_impressions_sum=1)
    assert len(r["queries"]) == 3
    assert [q["impressions"] for q in r["queries"]] == [12, 11, 10]


def test_threshold_drops_single_week_low_impressions(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    _write_snapshot(hist, "2026-W23", by_combo=[
        _combo("low imp query", PAGE, impressions=1),
    ])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is True
    assert r["queries"] == []


def test_threshold_keeps_two_weeks_low_impressions(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    _write_snapshot(hist, "2026-W22", by_combo=[_combo("q", PAGE, impressions=1)])
    _write_snapshot(hist, "2026-W23", by_combo=[_combo("q", PAGE, impressions=1)])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is False
    assert r["queries"][0]["impressions"] == 2
    assert r["queries"][0]["weeks_seen"] == 2


def test_no_match_is_fallback(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    _write_snapshot(hist, "2026-W23", by_combo=[
        _combo("unrelated", "https://navi.omcha.jp/products/otherasin0/", impressions=9),
    ])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is True


def test_missing_history_dir_is_fallback(tmp_path):
    r = build_context(ASIN, history_dir=str(tmp_path / "does_not_exist"), weeks=4)
    assert r["fallback"] is True
    assert r["weeks_scanned"] == 0


def test_gap_weeks_uses_latest_n_files_not_calendar_weeks(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    # W25 の次が W28 (欠番) でも、ファイル数ベースで直近 weeks 件を採用する
    _write_snapshot(hist, "2026-W20", by_combo=[_combo("old", PAGE, impressions=99)])
    _write_snapshot(hist, "2026-W23", by_combo=[_combo("a", PAGE, impressions=2)])
    _write_snapshot(hist, "2026-W24", by_combo=[_combo("a", PAGE, impressions=2)])
    _write_snapshot(hist, "2026-W25", by_combo=[_combo("a", PAGE, impressions=2)])
    _write_snapshot(hist, "2026-W28", by_combo=[_combo("a", PAGE, impressions=2)])
    r = build_context(ASIN, history_dir=str(hist), weeks=4, min_impressions_sum=100)
    # 直近4ファイル (W23,W24,W25,W28) のみ = "old" (W20) は含まれない
    assert r["weeks_scanned"] == 4
    assert r["queries"][0]["impressions"] == 8  # 2*4, "old" の 99 は含まれない
    assert r["queries"][0]["weeks_seen"] == 4


def test_by_combo_missing_key_and_broken_json_are_skipped(tmp_path):
    hist = tmp_path / "gsc_history"
    hist.mkdir()
    # by_combo キー欠落
    _write_snapshot(hist, "2026-W22", raw_text=json.dumps({"other": []}))
    # 壊れた JSON
    _write_snapshot(hist, "2026-W23", raw_text="{not valid json")
    # 正常データ
    _write_snapshot(hist, "2026-W24", by_combo=[
        _combo("query", PAGE, impressions=5),
    ])
    r = build_context(ASIN, history_dir=str(hist), weeks=4)
    assert r["fallback"] is False
    assert r["queries"][0]["impressions"] == 5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
