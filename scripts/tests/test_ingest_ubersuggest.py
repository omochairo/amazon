"""Ubersuggest CSV 取り込み (#2686 PR-C) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ingest_ubersuggest as U  # noqa: E402

REAL_RULES = ROOT.parent / "data" / "demand_query_rules.yaml"


def _rules(subject_exclusions=None, trailing_modifiers=None, suspect_threshold=500000):
    return {
        "version": 1,
        "subject_exclusions": subject_exclusions or {},
        "trailing_modifiers": trailing_modifiers or {},
        "suspect_volume_threshold": suspect_threshold,
    }


def _write_csv(path: pathlib.Path, header: list[str], rows: list[list[str]],
               encoding="utf-8", newline_style="\r\n"):
    lines = [",".join(header)] + [",".join(r) for r in rows]
    content = newline_style.join(lines) + newline_style
    path.write_bytes(content.encode(encoding))


# --------------------------------------------------------------------------
# CSV パーサ: 列エイリアス
# --------------------------------------------------------------------------

def test_singular_keyword_column_is_recognized(tmp_path):
    p = tmp_path / "ubersuggest https_example.com.csv"
    _write_csv(p, ["No", "Keyword", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "トミカ", "1000", "3", "50", "20", "http://example.com/x"]])
    rows, reason, fieldnames = U.read_csv_file(p)
    assert reason is None
    assert rows[0]["Keyword"] == "トミカ"


def test_search_volume_column_alias_is_recognized(tmp_path):
    p = tmp_path / "ubersuggest https_example.com.csv"
    _write_csv(p, ["No", "Keywords", "Search Volume", "Position"],
               [["1", "トミカ", "1000", "3"]])
    rows, reason, fieldnames = U.read_csv_file(p)
    assert reason is None
    cols = U.resolve_columns(fieldnames)
    assert cols["volume"] == "Search Volume"


# --------------------------------------------------------------------------
# Keywords 列が無いファイル
# --------------------------------------------------------------------------

def test_file_without_keywords_column_is_skipped_with_reason(tmp_path):
    p = tmp_path / "ubersuggest https___ozaki-swimming.com_.csv"
    _write_csv(p, ["No", "Title", "URL", "Est. Visits", "Backlinks"],
               [["1", "トップページ", "http://ozaki-swimming.com/", "100", "5"]])
    raw_rows, skipped = U.collect_csv_rows(tmp_path)
    assert raw_rows == []
    assert len(skipped) == 1
    assert skipped[0]["file"] == p.name
    assert skipped[0]["reason"]
    assert skipped[0]["columns"] == ["No", "Title", "URL", "Est. Visits", "Backlinks"]


# --------------------------------------------------------------------------
# サイト名抽出
# --------------------------------------------------------------------------

def test_site_name_from_filename():
    p = pathlib.Path("ubersuggest https_czech.hatenablog.com.csv")
    assert U.site_name_from_filename(p) == "czech.hatenablog.com"


def test_site_name_from_filename_with_trailing_slash_artifact():
    p = pathlib.Path("ubersuggest https___ozaki-swimming.com_.csv")
    assert U.site_name_from_filename(p) == "ozaki-swimming.com"


# --------------------------------------------------------------------------
# 重複排除 (空白除去キー + Volume 最大)
# --------------------------------------------------------------------------

def test_dedupe_uses_space_removed_key():
    raw_rows = [
        {"site": "a.com", "raw_query": "たまごっち みみっち", "volume": 10, "position": 5, "seo_difficulty": 20},
        {"site": "b.com", "raw_query": "たまごっちみみっち", "volume": 100, "position": 2, "seo_difficulty": 30},
    ]
    grouped = U.dedupe_rows(raw_rows)
    assert len(grouped) == 1
    entry = next(iter(grouped.values()))
    assert entry["volume"] == 100
    assert entry["raw_query"] == "たまごっちみみっち"
    assert entry["sites"] == {"a.com", "b.com"}


def test_dedupe_keeps_max_volume_rows_position_and_difficulty():
    raw_rows = [
        {"site": "a.com", "raw_query": "スクイーズ", "volume": 5000, "position": 9, "seo_difficulty": 40},
        {"site": "b.com", "raw_query": "スクイーズ", "volume": 12000, "position": 3, "seo_difficulty": 25},
    ]
    grouped = U.dedupe_rows(raw_rows)
    entry = grouped[U.bdk.normalize_key("スクイーズ")]
    assert entry["volume"] == 12000
    assert entry["position"] == 3
    assert entry["seo_difficulty"] == 25


# --------------------------------------------------------------------------
# 主題除外 (subject_exclusions) — 語ごと落とす
# --------------------------------------------------------------------------

def test_subject_exclusion_drops_keyword_with_category():
    rules = _rules(subject_exclusions={
        "character_list": {"label": "キャラ一覧・図鑑", "contains": ["キャラクター一覧"]},
    })
    raw_rows = [
        {"site": "a.com", "raw_query": "アンパンマン キャラクター一覧", "volume": 500,
         "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "アンパンマン ぬいぐるみ", "volume": 300,
         "position": 2, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    assert [k["query"] for k in rep["keywords"]] == ["アンパンマンぬいぐるみ"]
    assert len(rep["dropped_subject"]) == 1
    d = rep["dropped_subject"][0]
    assert d["query"] == "アンパンマン キャラクター一覧"
    assert d["categories"] == ["character_list"]
    assert d["volume"] == 500


def test_subject_exclusion_suffix_mode_matches_only_at_end():
    rules = _rules(subject_exclusions={
        "definition": {"label": "とは系定義", "suffix": ["とは"]},
    })
    raw_rows = [
        {"site": "a.com", "raw_query": "トミカとは", "volume": 100, "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "とはずがたり トミカ", "volume": 50, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    # 語頭・語中の「とは」は落とさない (suffix モードなので末尾のみ)
    assert [k["query"] for k in rep["keywords"]] == ["とはずがたりトミカ"]
    assert len(rep["dropped_subject"]) == 1
    assert rep["dropped_subject"][0]["query"] == "トミカとは"


# --------------------------------------------------------------------------
# 修飾語剥ぎ (trailing_modifiers) — 落とさず末尾だけ剥ぐ
# --------------------------------------------------------------------------

def test_trailing_modifier_is_stripped_only_at_end():
    rules = _rules(trailing_modifiers={
        "channel": {"label": "販路", "suffix": ["予約"]},
    })
    raw_rows = [
        {"site": "a.com", "raw_query": "たまごっちパラダイス 予約", "volume": 100,
         "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "予約サイト トミカ", "volume": 50,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    by_raw = {k["raw_query"]: k for k in rep["keywords"]}
    assert by_raw["たまごっちパラダイス 予約"]["query"] == "たまごっちパラダイス"
    assert by_raw["たまごっちパラダイス 予約"]["stripped_modifiers"] == ["予約"]
    # 語中の「予約」は剥がさない (末尾一致のみ)
    assert by_raw["予約サイト トミカ"]["query"] == U.bdk.normalize_key("予約サイト トミカ")
    assert by_raw["予約サイト トミカ"]["stripped_modifiers"] == []


def test_stripped_result_becoming_empty_is_dropped():
    rules = _rules(trailing_modifiers={
        "channel": {"label": "販路", "suffix": ["予約"]},
    })
    raw_rows = [
        {"site": "a.com", "raw_query": "予約", "volume": 10, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    assert rep["keywords"] == []
    assert rep["summary"]["dropped_empty_after_strip"] == 1


def test_realistic_example_strider_and_paradise():
    """課題本文の実例: ストライダー 後悔 → ストライダー / たまごっちパラダイス 予約 → たまごっちパラダイス。"""
    rules = _rules(trailing_modifiers={
        "concern": {"label": "懸念", "suffix": ["後悔"]},
        "channel": {"label": "販路", "suffix": ["予約"]},
    })
    raw_rows = [
        {"site": "a.com", "raw_query": "ストライダー 後悔", "volume": 100, "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "たまごっちパラダイス 予約", "volume": 100, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    queries = {k["raw_query"]: k["query"] for k in rep["keywords"]}
    assert queries["ストライダー 後悔"] == "ストライダー"
    assert queries["たまごっちパラダイス 予約"] == "たまごっちパラダイス"


# --------------------------------------------------------------------------
# Volume 0 / suspect_volume
# --------------------------------------------------------------------------

def test_zero_volume_is_not_dropped():
    rules = _rules()
    raw_rows = [{"site": "a.com", "raw_query": "レアな知育玩具", "volume": 0, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules)
    assert len(rep["keywords"]) == 1
    assert rep["keywords"][0]["volume"] == 0


def test_suspect_volume_flag_is_set_above_threshold_but_not_dropped():
    rules = _rules(suspect_threshold=500000)
    raw_rows = [{"site": "a.com", "raw_query": "知育 村", "volume": 1000000, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules)
    assert len(rep["keywords"]) == 1
    k = rep["keywords"][0]
    assert k["suspect_volume"] is True
    assert rep["summary"]["suspect_volume"] == 1


def test_normal_volume_is_not_flagged_suspect():
    rules = _rules(suspect_threshold=500000)
    raw_rows = [{"site": "a.com", "raw_query": "トミカ", "volume": 12000, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules)
    assert rep["keywords"][0]["suspect_volume"] is False


# --------------------------------------------------------------------------
# WP 突き合わせ (impression 加重平均 position)
# --------------------------------------------------------------------------

def _wp_history_file(tmp_path, rows):
    p = tmp_path / "gsc_wp_by_query.jsonl"
    lines = []
    for r in rows:
        rec = {
            "query": r["query"],
            "date": r.get("date", "2026-05-01"),
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "position": r["position"],
            "ctr": (r["clicks"] / r["impressions"]) if r["impressions"] else 0.0,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_wp_crossmatch_position_is_impression_weighted(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "トミカ", "date": "2026-05-01", "impressions": 1000, "clicks": 200, "position": 1.0},
        {"query": "トミカ", "date": "2026-05-02", "impressions": 10, "clicks": 1, "position": 20.0},
    ])
    stats = U.bdk.load_wp_rank_stats(history)
    rules = _rules()
    raw_rows = [{"site": "a.com", "raw_query": "トミカ", "volume": 1000, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules, wp_rank_stats=stats)
    k = rep["keywords"][0]
    assert k["wp_impressions"] == 1010
    assert k["wp_clicks"] == 201
    expected_pos = (1000 * 1.0 + 10 * 20.0) / 1010
    assert abs(k["wp_position"] - round(expected_pos, 1)) < 0.05
    assert k["wp_position"] < 3.0  # 単純平均 (10.5) なら閾値超えのはず


def test_wp_crossmatch_flags_rank_guard_but_does_not_drop(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "アンパンマンシール", "impressions": 500, "clicks": 220, "position": 1.1},
    ])
    stats = U.bdk.load_wp_rank_stats(history)
    rules = _rules()
    raw_rows = [{"site": "a.com", "raw_query": "アンパンマンシール", "volume": 500, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules, wp_rank_stats=stats)
    assert len(rep["keywords"]) == 1  # 除外しない
    k = rep["keywords"][0]
    assert k["wp_rank_guard"] is True
    assert rep["summary"]["wp_rank_guard_flagged"] == 1
    assert rep["summary"]["wp_duplicate"] == 1


def test_wp_crossmatch_no_match_leaves_wp_fields_none():
    rules = _rules()
    raw_rows = [{"site": "a.com", "raw_query": "未知の玩具語", "volume": 100, "position": 1, "seo_difficulty": 10}]
    rep = U.build(raw_rows, rules, wp_rank_stats={})
    k = rep["keywords"][0]
    assert k["wp_impressions"] is None
    assert k["wp_rank_guard"] is False
    assert rep["summary"]["wp_duplicate"] == 0


# --------------------------------------------------------------------------
# CRLF・BOM 付き CSV
# --------------------------------------------------------------------------

def test_crlf_csv_is_read_correctly(tmp_path):
    p = tmp_path / "ubersuggest https_example.com.csv"
    _write_csv(p, ["No", "Keywords", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "トミカ", "1000", "3", "50", "20", "http://example.com/x"],
                ["2", "レゴ", "2000", "1", "80", "15", "http://example.com/y"]],
               newline_style="\r\n")
    raw_rows, skipped = U.collect_csv_rows(tmp_path)
    assert skipped == []
    assert len(raw_rows) == 2
    assert {r["raw_query"] for r in raw_rows} == {"トミカ", "レゴ"}


def test_bom_prefixed_csv_is_read_correctly(tmp_path):
    p = tmp_path / "ubersuggest https_example.com.csv"
    _write_csv(p, ["No", "Keywords", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "トミカ", "1000", "3", "50", "20", "http://example.com/x"]])
    # BOM を先頭に手動付与
    raw = p.read_bytes()
    p.write_bytes(b"\xef\xbb\xbf" + raw)
    rows, reason, fieldnames = U.read_csv_file(p)
    assert reason is None
    assert fieldnames[0] == "No"  # BOM が列名に混入していない
    assert rows[0]["Keywords"] == "トミカ"


# --------------------------------------------------------------------------
# ネットワークに出ないこと
# --------------------------------------------------------------------------

def test_run_does_not_touch_network(tmp_path, monkeypatch):
    import socket

    def _blocked(*a, **kw):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", _blocked)

    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    _write_csv(csv_dir / "ubersuggest https_example.com.csv",
               ["No", "Keywords", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "トミカ", "1000", "3", "50", "20", "http://example.com/x"]])
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(_rules(), allow_unicode=True), encoding="utf-8")
    out_path = tmp_path / "out.json"

    result = U.run(csv_dir, rules_path, out_path, wp_history_path=None, dry_run=True)
    assert result["summary"]["kept_keywords"] == 1


# --------------------------------------------------------------------------
# 実データの語彙ルールで既知パターンを固定する (存在すれば)
# --------------------------------------------------------------------------

def test_real_rules_file_is_loadable_and_two_tier():
    if not REAL_RULES.exists():
        pytest.skip("real rules file not present")
    rules = U.load_rules(REAL_RULES)
    assert "subject_exclusions" in rules
    assert "trailing_modifiers" in rules
    # 混同していないことの固定: 口コミは trailing_modifiers 側、
    # キャラクター一覧の主題語 (「一覧」) は subject_exclusions 側。
    subj_terms = set()
    for cat in rules["subject_exclusions"].values():
        subj_terms.update(cat.get("contains") or [])
        subj_terms.update(cat.get("suffix") or [])
    mod_terms = set()
    for cat in rules["trailing_modifiers"].values():
        mod_terms.update(cat.get("suffix") or [])
    assert "口コミ" in mod_terms and "口コミ" not in subj_terms
    assert "一覧" in subj_terms and "一覧" not in mod_terms
