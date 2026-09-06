"""キーワード台帳 (omcha-ops#97 P0/P1) の検査。

**守りたいのは4つ。**

1. **未測定と SV=0 を混ぜないこと。** `monthly_searches` が空なら未測定であって
   「需要が無い」ではない。ここを取り違えると施策を落とす (実測: `児童 生徒 違い`
   は外部 0 / GSC 1,750 impr)。
2. **表記ゆれを二重に数えないこと。** 「箱根 子連れ」と「箱根子連れ」は別レコードで
   返ってくるので、norm で畳まないと需要が倍に見える。
3. **append-only であること。** 再取得しても当時の数字が消えない。
4. **両サイトが同じ語を primary にしたら見つかること。** これが台帳を共有する理由。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import kw_ledger as K  # noqa: E402


def _batch(tmp_path, results, block="b", fetched="2026-09-07"):
    p = tmp_path / "batch.json"
    p.write_text(json.dumps({"loc_id": 2392, "language": "ja", "block": block,
                             "fetched": fetched, "results": results},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path, *argv):
    ext = tmp_path / "external.jsonl"
    asg = tmp_path / "assign.jsonl"
    return K.main(["--external", str(ext), "--assign", str(asg), *argv])


def _rows(tmp_path):
    return K.read_jsonl(tmp_path / "external.jsonl")


def test_months_normalized():
    got = K.months([{"period": "202507", "search_volume": 1900}])
    assert got == [{"month": "2025-07", "volume": 1900}]


def test_measured_flag_separates_zero_from_unmeasured(tmp_path):
    src = _batch(tmp_path, [
        {"keyword": "測れた語", "search_volume": 100, "seo_difficulty": 20,
         "monthly_searches": [{"period": "202608", "search_volume": 100}]},
        # SV=0 かつ monthly_searches が空 = 未測定。sd 17 は機械的に入るだけ。
        {"keyword": "未測定の語", "search_volume": 0, "seo_difficulty": 17,
         "monthly_searches": []},
    ])
    assert _run(tmp_path, "merge", str(src)) == 0
    by_kw = {r["keyword"]: r for r in _rows(tmp_path)}
    assert by_kw["測れた語"]["measured"] is True
    assert by_kw["未測定の語"]["measured"] is False, "SV=0 を「測った結果 0」と読まない"


def test_norm_folds_spacing_variants(tmp_path):
    src = _batch(tmp_path, [
        {"keyword": "箱根 子連れ", "search_volume": 1600,
         "monthly_searches": [{"period": "202608", "search_volume": 1600}]},
        {"keyword": "箱根子連れ", "search_volume": 1600,
         "monthly_searches": [{"period": "202608", "search_volume": 1600}]},
    ])
    _run(tmp_path, "merge", str(src))
    rows = _rows(tmp_path)
    assert len(rows) == 1, "2件目は同じ norm なので skip される (足すと二重に数える)"
    assert K.normalize_key("箱根 子連れ") == K.normalize_key("箱根子連れ")


def test_merge_is_append_only(tmp_path):
    src = _batch(tmp_path, [{"keyword": "語", "search_volume": 100,
                             "monthly_searches": [{"period": "202608",
                                                   "search_volume": 100}]}],
                 fetched="2026-06-01")
    _run(tmp_path, "merge", str(src))
    src2 = _batch(tmp_path, [{"keyword": "語", "search_volume": 250,
                              "monthly_searches": [{"period": "202609",
                                                    "search_volume": 250}]}],
                  fetched="2026-09-07")
    _run(tmp_path, "merge", str(src2), "--refresh")

    rows = _rows(tmp_path)
    assert len(rows) == 2, "再取得は追記。当時の数字を消さない"
    cur = K.latest(rows)
    assert cur[("語", 2392, "ja")]["sv"] == 250, "最新は fetched_at が大きい方"
    assert any(r["sv"] == 100 for r in rows), "古い行が残っている"


def test_merge_skips_known_word_without_refresh(tmp_path):
    src = _batch(tmp_path, [{"keyword": "語", "search_volume": 100,
                             "monthly_searches": []}])
    _run(tmp_path, "merge", str(src))
    _run(tmp_path, "merge", str(src))
    assert len(_rows(tmp_path)) == 1


def test_todo_lists_only_unfetched(tmp_path, capsys):
    src = _batch(tmp_path, [{"keyword": "取った語", "search_volume": 10,
                             "monthly_searches": []}])
    _run(tmp_path, "merge", str(src))
    kws = tmp_path / "kw.txt"
    kws.write_text("## ブロック\n取った語\nまだの語\n取った語\n", encoding="utf-8")
    capsys.readouterr()
    _run(tmp_path, "todo", str(kws))
    out = capsys.readouterr().out.splitlines()
    assert out == ["まだの語"]


def test_todo_refresh_stale(tmp_path, capsys):
    src = _batch(tmp_path, [{"keyword": "古い語", "search_volume": 10,
                             "monthly_searches": []}], fetched="2020-01-01")
    _run(tmp_path, "merge", str(src))
    kws = tmp_path / "kw.txt"
    kws.write_text("古い語\n", encoding="utf-8")
    capsys.readouterr()
    _run(tmp_path, "todo", str(kws))
    assert capsys.readouterr().out.strip() == "", "既定では再取得しない"
    _run(tmp_path, "todo", str(kws), "--refresh-stale")
    assert capsys.readouterr().out.strip() == "古い語"


def test_import_navi_marks_unknown_measurement(tmp_path):
    src = tmp_path / "ubersuggest_demand.json"
    src.write_text(json.dumps({
        "generated_at": "2026-08-10T10:00:25Z",
        "keywords": [
            {"query": "たまごっち", "raw_query": "たまごっち", "volume": 1000000.0,
             "sites": ["www.toysrus.co.jp"], "seo_difficulty": 50.0,
             "suspect_volume": True},
            {"query": "ボリューム無し", "raw_query": "ボリューム無し", "volume": 0.0,
             "sites": ["x.example"], "seo_difficulty": 17.0},
        ]}, ensure_ascii=False), encoding="utf-8")
    _run(tmp_path, "import-navi", "--src", str(src))
    rows = {r["keyword"]: r for r in _rows(tmp_path)}
    assert rows["たまごっち"]["fetched_at"] == "2026-08-10", "CSV の取得日を捏造しない"
    assert rows["たまごっち"]["extra"]["suspect_volume"] is True
    for r in rows.values():
        assert r["measured_unknown"] is True, "CSV 由来は測定の有無を判定していない"
    assert rows["ボリューム無し"]["measured"] is False


def test_import_cache_keeps_block_and_fetched(tmp_path):
    src = tmp_path / "cache.jsonl"
    with io.open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"keyword": "壬生町おもちゃ博物館", "search_volume": 22200,
                            "seo_difficulty": 24, "loc_id": 2392, "language": "ja",
                            "block": "trip", "fetched": "2026-09-06",
                            "monthly_searches": [{"period": "202507",
                                                  "search_volume": 22200}]},
                           ensure_ascii=False) + "\n")
    _run(tmp_path, "import-cache", "--src", str(src))
    r = _rows(tmp_path)[0]
    assert r["block"] == "trip" and r["fetched_at"] == "2026-09-06"
    assert r["monthly_searches"] == [{"month": "2025-07", "volume": 22200}]
    assert r["source"] == "mcp:keyword_overview"


def test_assign_conflict_is_found(tmp_path, capsys):
    _run(tmp_path, "assign", "箱根 子連れ", "--site", "omcha",
         "--url", "https://omcha.jp/a/")
    _run(tmp_path, "assign", "箱根子連れ", "--site", "navi",
         "--url", "https://navi.omcha.jp/b/")
    capsys.readouterr()
    _run(tmp_path, "report")
    out = capsys.readouterr().out
    assert "衝突 (両サイトが primary) — 1 件" in out, \
        "表記ゆれ違いでも同じ norm なら衝突として出る"


def test_assign_avoid_is_not_a_conflict(tmp_path, capsys):
    _run(tmp_path, "assign", "箱根 子連れ", "--site", "omcha")
    _run(tmp_path, "assign", "箱根 子連れ", "--site", "navi", "--role", "avoid")
    capsys.readouterr()
    _run(tmp_path, "report")
    assert "衝突 (両サイトが primary) — 0 件" in capsys.readouterr().out


def test_assign_latest_decision_wins(tmp_path):
    _run(tmp_path, "assign", "語", "--site", "navi", "--role", "primary",
         "--decided-at", "2026-01-01")
    _run(tmp_path, "assign", "語", "--site", "navi", "--role", "avoid",
         "--decided-at", "2026-09-07")
    cur = K.latest_assign(K.read_jsonl(tmp_path / "assign.jsonl"))
    assert cur[("語", "navi")]["role"] == "avoid"
    assert len(K.read_jsonl(tmp_path / "assign.jsonl")) == 2, "判断の履歴も残す"


def test_report_flags_stale_only_for_assigned_words(tmp_path, capsys):
    src = _batch(tmp_path, [
        {"keyword": "採用した語", "search_volume": 10, "monthly_searches": []},
        {"keyword": "採用していない語", "search_volume": 10, "monthly_searches": []},
    ], fetched="2020-01-01")
    _run(tmp_path, "merge", str(src))
    _run(tmp_path, "assign", "採用した語", "--site", "omcha")
    capsys.readouterr()
    _run(tmp_path, "report")
    out = capsys.readouterr().out
    assert "外部データが古い — 1 件" in out, "全件ではなく採否を分けた語だけを見る"
    assert "採用していない語" not in out.split("## 未測定")[0]


def test_report_lists_unmeasured_with_a_warning(tmp_path, capsys):
    src = _batch(tmp_path, [{"keyword": "未測定の語", "search_volume": 0,
                             "monthly_searches": []}])
    _run(tmp_path, "merge", str(src))
    capsys.readouterr()
    _run(tmp_path, "report")
    out = capsys.readouterr().out
    assert "未測定 (monthly_searches が空) — 1 語" in out
    assert "需要が無いという意味ではない" in out


def test_stats_runs_on_empty_ledger(tmp_path, capsys):
    _run(tmp_path, "stats")
    assert "external.jsonl: 0 行" in capsys.readouterr().out


def test_unknown_site_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        _run(tmp_path, "assign", "語", "--site", "wordpress")


def _ledger_with_csv(tmp_path, rows):
    src = tmp_path / "ubersuggest_demand.json"
    src.write_text(json.dumps({"generated_at": "2026-08-10T00:00:00Z",
                               "keywords": rows}, ensure_ascii=False),
                   encoding="utf-8")
    _run(tmp_path, "import-navi", "--src", str(src))


def test_refetch_queue_orders_and_excludes(tmp_path, capsys):
    """1 日 100 レポートしか引けないので、枠の使い道が成果を決める。"""
    _ledger_with_csv(tmp_path, [
        {"query": "大きい語", "raw_query": "大きい語", "volume": 5000.0},
        {"query": "小さい語", "raw_query": "小さい語", "volume": 10.0},
        # CSV の集計崩れ。測り直す対象ではなく CSV 側が壊れている印
        {"query": "崩れた語", "raw_query": "崩れた語", "volume": 1000000.0,
         "suspect_volume": True},
        # WP が上位で取っている語。navi では使えないので枠を使わない
        {"query": "WPの語", "raw_query": "WPの語", "volume": 9000.0},
    ])
    wp = tmp_path / "wp_demand.jsonl"
    with io.open(wp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"kind": "wp_query_window", "date": "2026-08-30"}) + "\n")
        f.write(json.dumps({"query": "WPの語", "norm": K.normalize_key("WPの語"),
                            "clicks": 400.0, "impressions": 5000.0,
                            "position": 1.4}, ensure_ascii=False) + "\n")

    capsys.readouterr()
    _run(tmp_path, "refetch-queue", "--limit", "10", "--wp-demand", str(wp))
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["大きい語", "小さい語"], \
        "suspect と WP 既得を外し、CSV volume の降順で出す"
    assert "suspect=1" in cap.err and "wp既得=1" in cap.err, "除外の根拠を出す"


def test_refetch_queue_respects_daily_limit(tmp_path, capsys):
    _ledger_with_csv(tmp_path, [
        {"query": "語%d" % i, "raw_query": "語%d" % i, "volume": float(100 - i)}
        for i in range(5)])
    capsys.readouterr()
    _run(tmp_path, "refetch-queue", "--limit", "2", "--wp-demand", "")
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["語0", "語1"]
    assert "残り 3" in cap.err


def test_refetch_queue_skips_already_measured(tmp_path, capsys):
    """MCP で測り直した語は二度と queue に出ない (measured_unknown が消えるため)。"""
    _ledger_with_csv(tmp_path, [{"query": "語", "raw_query": "語", "volume": 100.0}])
    src = _batch(tmp_path, [{"keyword": "語", "search_volume": 40,
                             "monthly_searches": [{"period": "202608",
                                                   "search_volume": 40}]}],
                 fetched="2026-09-07")
    _run(tmp_path, "merge", str(src), "--refresh")
    capsys.readouterr()
    _run(tmp_path, "refetch-queue", "--wp-demand", "")
    assert capsys.readouterr().out.strip() == ""


def test_refetch_queue_keep_list(tmp_path, capsys):
    """枠は「navi が実際に使う語」に割り当てる (5,292語=53日 → 158語=2日)。"""
    _ledger_with_csv(tmp_path, [
        {"query": "使う語", "raw_query": "使う語", "volume": 100.0},
        {"query": "使わない語", "raw_query": "使わない語", "volume": 9999.0},
        {"query": "元クエリ側で一致", "raw_query": "元クエリ側で一致", "volume": 50.0},
    ])
    keep = tmp_path / "demand_keywords.json"
    keep.write_text(json.dumps({"keywords": [
        {"keyword": "使う語", "source_queries": []},
        {"keyword": "別の語", "source_queries": ["元クエリ 側で一致"]},
    ]}, ensure_ascii=False), encoding="utf-8")

    capsys.readouterr()
    _run(tmp_path, "refetch-queue", "--wp-demand", "", "--keep-list", str(keep))
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["使う語", "元クエリ側で一致"], \
        "source_queries 側の表記ゆれでも norm が一致すれば残る"
    assert "リスト外=1" in cap.err
