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
# 店名複合条件マッチ (match_brand_composite) 単体
# --------------------------------------------------------------------------

def _brand_composite_defs():
    section = {
        "store_navigational": {
            "label": "店舗ナビゲーショナル",
            "brand_composite": {
                "brands": ["ボーネルンド"],
                "modifiers": ["店舗", "クーポン"],
            },
        },
    }
    return U._flatten_brand_composite(section)


def test_match_brand_composite_exact_brand_matches():
    defs = _brand_composite_defs()
    assert U.match_brand_composite(U.bdk.normalize_key("ボーネルンド"), defs) != []


def test_match_brand_composite_brand_plus_modifier_matches():
    defs = _brand_composite_defs()
    assert U.match_brand_composite(U.bdk.normalize_key("ボーネルンド 店舗"), defs) != []


def test_match_brand_composite_brand_plus_other_word_does_not_match():
    defs = _brand_composite_defs()
    assert U.match_brand_composite(U.bdk.normalize_key("ボーネルンド おもちゃ"), defs) == []


def test_match_brand_composite_no_brand_does_not_match():
    defs = _brand_composite_defs()
    assert U.match_brand_composite(U.bdk.normalize_key("トミカ おもちゃ"), defs) == []


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
# 誤除外の回帰テスト (2026-08-10 owner レビューで発覚、L1 は保守的に倒す)
#
# 「図鑑」「ランド」「パーク」「幼稚園」「保育園」の断片トークン/広すぎる語が
# 実在商品・商品になりうる語を subject_exclusions で誤除外していた。
# 誤除外は L2 実査で回復できない (非対称なコスト) ので、二度と混入しないよう
# 実ルールファイルに対して固定する。
# --------------------------------------------------------------------------

def test_randoseru_is_not_dropped_by_facility_rule():
    """「ランド」断片が「ランドセル」(商品カテゴリそのもの) を誤爆させていた回帰。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "a.com", "raw_query": "アクタスランドセル", "volume": 100, "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "ランドセルリメイク 後悔", "volume": 100, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "アクタスランドセル" not in dropped_queries
    assert "ランドセルリメイク 後悔" not in dropped_queries


def test_papercraft_is_not_dropped_by_facility_rule():
    """「パーク」断片が「ペーパークラフト」を誤爆させていた回帰。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "a.com", "raw_query": "ペーパークラフト作り方", "volume": 100, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    # 「作り方」は diy カテゴリで別途落ちるので、facility カテゴリで落ちて
    # いないことをカテゴリ単位で確認する。
    d = next((d for d in rep["dropped_subject"] if d["query"] == "ペーパークラフト作り方"), None)
    assert d is not None, "作り方 (diy) では落ちる想定"
    assert "facility" not in d["categories"]


def test_product_name_containing_zukan_is_not_dropped():
    """「図鑑」トークンが実在商品「アンパンマンことば図鑑プレミアム」を誤除外していた回帰。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "a.com", "raw_query": "アンパンマンことば図鑑プレミアム", "volume": 1000,
         "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "英語ことば図鑑5000", "volume": 500,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "アンパンマンことば図鑑プレミアム" in kept
    assert "英語ことば図鑑5000" in kept


def test_hoikuen_query_is_not_dropped():
    """「保育園」が「保育園 シール貼り」のような玩具需要語を誤除外していた回帰。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "a.com", "raw_query": "保育園 シール貼り", "volume": 100, "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "カレンダー保育園", "volume": 50, "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "保育園 シール貼り" in kept
    assert "カレンダー保育園" in kept


def test_facility_rule_still_drops_specific_facility_names():
    """断片トークンを削っても、具体名の施設クエリは引き続き落ちること。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "a.com", "raw_query": "レゴランド大阪 名古屋 違い", "volume": 100,
         "position": 1, "seo_difficulty": 10},
        {"site": "a.com", "raw_query": "アンパンマンミュージアム 名古屋", "volume": 100,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "レゴランド大阪 名古屋 違い" in dropped_queries
    assert "アンパンマンミュージアム 名古屋" in dropped_queries


# --------------------------------------------------------------------------
# 実データの語彙ルールで既知パターンを固定する (存在すれば)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# サイト単位の採否 (excluded_sites, #2686 PR-D)
# --------------------------------------------------------------------------

def test_excluded_site_row_is_dropped_and_reported():
    rules = _rules()
    rules["excluded_sites"] = {"p-bandai.jp": {"reason": "バンダイ系除外 (owner判断)"}}
    raw_rows = [
        {"site": "p-bandai.jp", "raw_query": "鬼滅の刃 フィギュア", "volume": 1830000,
         "position": 1, "seo_difficulty": 10},
    ]
    excluded_report = U.compute_excluded_sites_report(raw_rows, rules)
    assert excluded_report == [{
        "site": "p-bandai.jp",
        "reason": "バンダイ系除外 (owner判断)",
        "keyword_rows": 1,
        "volume_sum": 1830000,
    }]
    filtered = U.filter_excluded_site_rows(raw_rows, {"p-bandai.jp"})
    assert filtered == []
    rep = U.build(filtered, rules)
    assert rep["keywords"] == []


def test_word_surviving_via_non_excluded_site_is_kept():
    """「ベイブレードx」型の回帰: takaratomymall (採用) と toysrus (採用) の
    両方に出る語は、bandai-hobby (除外) にも出ていたとしても残る。"""
    rules = _rules()
    rules["excluded_sites"] = {"bandai-hobby.net": {"reason": "バンダイ系除外"}}
    raw_rows = [
        {"site": "bandai-hobby.net", "raw_query": "ガンプラ", "volume": 368000,
         "position": 1, "seo_difficulty": 10},
        {"site": "p-bandai.jp", "raw_query": "ガンプラ", "volume": 300000,
         "position": 2, "seo_difficulty": 10},
        {"site": "www.toysrus.co.jp", "raw_query": "ガンプラ", "volume": 5000,
         "position": 5, "seo_difficulty": 10},
    ]
    filtered = U.filter_excluded_site_rows(raw_rows, {"bandai-hobby.net"})
    rep = U.build(filtered, rules)
    assert len(rep["keywords"]) == 1
    k = rep["keywords"][0]
    # bandai-hobby (Volume最大368000) は除外されているので、残った行のうち
    # 最大 (p-bandai の300000) が採用される。p-bandai は excluded_sites に
    # 無いのでこのテストでは採用サイト扱い。
    assert k["volume"] == 300000
    assert "bandai-hobby.net" not in k["sites"]


def test_word_fully_from_excluded_sites_disappears_entirely():
    """全出典が除外サイトだった語 (「仮面ライダー」型) は grouped から消える。"""
    rules = _rules()
    rules["excluded_sites"] = {
        "p-bandai.jp": {"reason": "除外"},
        "bandai-hobby.net": {"reason": "除外"},
    }
    raw_rows = [
        {"site": "p-bandai.jp", "raw_query": "仮面ライダー", "volume": 450000,
         "position": 1, "seo_difficulty": 10},
        {"site": "bandai-hobby.net", "raw_query": "仮面ライダー", "volume": 400000,
         "position": 1, "seo_difficulty": 10},
    ]
    filtered = U.filter_excluded_site_rows(raw_rows, {"p-bandai.jp", "bandai-hobby.net"})
    rep = U.build(filtered, rules)
    assert rep["keywords"] == []
    assert rep["dropped_subject"] == []  # 主題除外ではなくサイト除外で消えた


def test_run_wires_excluded_sites_into_output(tmp_path):
    """run() が excluded_sites を握り潰さず出力 JSON に記録すること。"""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    _write_csv(csv_dir / "ubersuggest https_p-bandai.jp.csv",
               ["No", "Keywords", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "鬼滅の刃 フィギュア", "1830000", "1", "50", "20", "http://p-bandai.jp/x"]])
    _write_csv(csv_dir / "ubersuggest https_www.toysrus.co.jp.csv",
               ["No", "Keywords", "Volume", "Position", "Est. Visits", "Seo Difficulty", "Ranking Url"],
               [["1", "トミカ", "1000", "3", "50", "20", "http://toysrus/x"]])
    rules_data = _rules()
    rules_data["excluded_sites"] = {"p-bandai.jp": {"reason": "バンダイ系除外 (owner判断)"}}
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules_data, allow_unicode=True), encoding="utf-8")
    out_path = tmp_path / "out.json"

    result = U.run(csv_dir, rules_path, out_path, wp_history_path=None, dry_run=True)
    assert result["excluded_sites"] == [{
        "site": "p-bandai.jp",
        "reason": "バンダイ系除外 (owner判断)",
        "keyword_rows": 1,
        "volume_sum": 1830000,
    }]
    kept = {k["raw_query"] for k in result["keywords"]}
    assert "鬼滅の刃 フィギュア" not in kept
    assert "トミカ" in kept


# --------------------------------------------------------------------------
# 店舗ナビゲーショナル (store_navigational, #2686 PR-D)
# --------------------------------------------------------------------------

def test_store_navigational_drops_store_admin_queries():
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.toysrus.co.jp", "raw_query": "トイザらス クーポン", "volume": 2900,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.toysrus.co.jp", "raw_query": "トイザらス営業時間", "volume": 1600,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド 店舗", "volume": 1300,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "トイザらス クーポン" in dropped_queries
    assert "トイザらス営業時間" in dropped_queries
    assert "ボーネルンド 店舗" in dropped_queries
    for d in rep["dropped_subject"]:
        assert "store_navigational" in d["categories"]


def test_store_navigational_fragment_does_not_misfire_inside_product_name():
    """短い断片ではなくサイト名/運営語そのものなので、商品名の内側で誤爆しないこと。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.toysrus.co.jp", "raw_query": "アンパンマン 砂場 セット", "volume": 1600,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.toysrus.co.jp", "raw_query": "たまごっちパラダイス みみっち", "volume": 100,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "アンパンマン 砂場 セット" in kept
    assert "たまごっちパラダイス みみっち" in kept


# --------------------------------------------------------------------------
# 店名の複合条件 (brand_composite, 2026-08-10 owner レビューで単純 contains
# から変更。ボーネルンド/タカラトミーは「店名である前に玩具ブランド」)
# --------------------------------------------------------------------------

def test_brand_alone_is_dropped():
    """クエリ全体が店名だけ → 落とす。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド", "volume": 60500,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "ボーネルンド" in dropped_queries


def test_brand_plus_product_word_is_not_dropped():
    """「ボーネルンド おもちゃ」型の回帰: 店名+商品語は落とさず L2 に送る
    (単純 contains だった初版はこれを誤除外していた)。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド おもちゃ", "volume": 8100,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド の おもちゃ", "volume": 8100,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "ボーネルンド おもちゃ" in kept
    assert "ボーネルンド の おもちゃ" in kept


def test_bornelund_looping_matches_existing_wp_demand_keyword():
    """data/demand_keywords.json に「ボーネルンドルーピング」が WP GSC 由来の
    正規需要語として既に存在する。Ubersuggest 側から同じ語が来ても落とさない
    こと (誤除外が既存の正規需要語と衝突しないことの回帰)。"""
    rules = U.load_rules(REAL_RULES)
    # 空白の有無は dedupe_rows で同一キーに正規化される (normalize_key が空白を
    # 除去する) ので、片方だけでも落ちないことを確認すれば十分。
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド ルーピング", "volume": 1000,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    assert len(rep["keywords"]) == 1
    assert rep["keywords"][0]["query"] == U.bdk.normalize_key("ボーネルンドルーピング")
    assert not rep["dropped_subject"]


def test_brand_plus_store_modifier_is_dropped():
    """「トイザらス 店舗」「近くのトイザらス」「トイザらス ブラックフライデー」
    のような店名+店舗運営語は引き続き落ちること。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.toysrus.co.jp", "raw_query": "トイザらス 店舗", "volume": 1300,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.toysrus.co.jp", "raw_query": "近くのトイザらス", "volume": 1000,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.toysrus.co.jp", "raw_query": "トイザらス ブラックフライデー", "volume": 1000,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "トイザらス 店舗" in dropped_queries
    assert "近くのトイザらス" in dropped_queries
    assert "トイザらス ブラックフライデー" in dropped_queries


def test_brand_plus_place_name_is_not_dropped_sent_to_l2():
    """「ボーネルンド 大阪」型: 地名は modifiers に含めていないので落とさず
    L2 に送る (施設クエリの可能性が高いが、L1 では断定しない)。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド 大阪", "volume": 4400,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "ボーネルンド 大阪" in kept


# --------------------------------------------------------------------------
# 施設語の拡充 (facility, #2686 PR-D)
# --------------------------------------------------------------------------

def test_facility_additions_drop_bornelund_park_queries():
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "デンパーク 水遊び", "volume": 390,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.bornelund.co.jp", "raw_query": "ペップキッズ郡山", "volume": 9900,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped_queries = {d["query"] for d in rep["dropped_subject"]}
    assert "デンパーク 水遊び" in dropped_queries
    assert "ペップキッズ郡山" in dropped_queries


def test_kidokido_facility_brand_is_dropped():
    """「キドキド」はボーネルンドの屋内遊び場プログラム名。facility で落ちる
    こと (store_navigational の brand_composite ではなく facility 側)。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.bornelund.co.jp", "raw_query": "キドキド", "volume": 2900,
         "position": 1, "seo_difficulty": 10},
        {"site": "www.bornelund.co.jp", "raw_query": "ボーネルンド キドキド", "volume": 2900,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    dropped = {d["query"]: d for d in rep["dropped_subject"]}
    assert "キドキド" in dropped
    assert "facility" in dropped["キドキド"]["categories"]
    assert "ボーネルンド キドキド" in dropped
    assert "facility" in dropped["ボーネルンド キドキド"]["categories"]


def test_sandbox_set_product_is_not_dropped_by_facility_rule():
    """「砂場」は実在商品 (アンパンマン砂場セット) と共起するため facility に
    入れていない (L2 に送る判断)。誤除外していないことを固定する。"""
    rules = U.load_rules(REAL_RULES)
    raw_rows = [
        {"site": "www.toysrus.co.jp", "raw_query": "アンパンマン砂場セット", "volume": 1600,
         "position": 1, "seo_difficulty": 10},
    ]
    rep = U.build(raw_rows, rules)
    kept = {k["raw_query"] for k in rep["keywords"]}
    assert "アンパンマン砂場セット" in kept


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
