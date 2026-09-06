"""WP 需要ブリッジ (#5941 系 / omcha-ops#97 P2) の検査。

**守りたいのは2つ。**

1. **出力が既存の消費者にそのまま読めること。** ブリッジは日次レーン
   (`gsc_wp_by_query.jsonl`) と同じフィールド名で出すので、
   `load_wp_rank_stats` / `wp_history_last_date` / `detect_demand_gaps.
   load_gsc_impressions` はコードを変えずに読めなければならない。
   ここが崩れると host crowding ガードが**例外を出さずに**当たらなくなる。
2. **鮮度を証明できない派生物を出さないこと。** `date` (= 窓の最終日) が無いまま
   渡すと、navi 側は「壊れている」ではなく「古いだけ」を検出できない (#5107 と同じ型)。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_demand_keywords as B  # noqa: E402
import detect_demand_gaps as D  # noqa: E402
import export_wp_demand as E  # noqa: E402


def _write(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


@pytest.fixture()
def sources(tmp_path):
    src = _write(tmp_path / "by_query.jsonl", [
        # 同じ語が分かち書き違いで2行 → 1行に合流する
        {"week": "2026-W34", "query": "トミカ 収納", "clicks": 10,
         "impressions": 100, "position": 2.0},
        {"week": "2026-W35", "query": "トミカ収納", "clicks": 5,
         "impressions": 300, "position": 6.0},
        # WP が取れている語 (ガードが外すべきもの)
        {"week": "2026-W35", "query": "アンパンマンシール 100均", "clicks": 386,
         "impressions": 2000, "position": 1.2},
        # 窓の外 (weeks=2 なら W33 は入らない)
        {"week": "2026-W33", "query": "トミカ 収納", "clicks": 99,
         "impressions": 999, "position": 1.0},
        # imp の裾 → min_impressions で落ちる
        {"week": "2026-W35", "query": "裾の語", "clicks": 0,
         "impressions": 2, "position": 30.0},
    ])
    totals = _write(tmp_path / "totals.jsonl", [
        {"week": "2026-W33", "week_start": "2026-08-10", "week_end": "2026-08-16"},
        {"week": "2026-W34", "week_start": "2026-08-17", "week_end": "2026-08-23"},
        {"week": "2026-W35", "week_start": "2026-08-24", "week_end": "2026-08-30"},
    ])
    return src, totals


def test_normalize_key_is_not_reimplemented():
    # コピーではなく import であること。ここが別実装に戻ると照合が静かに外れる。
    assert E.normalize_key is B.normalize_key


def test_build_aggregates_over_the_window(sources):
    src, totals = sources
    meta, rows = E.build(src, totals, weeks=2, min_impressions=3)

    by_norm = {r["norm"]: r for r in rows}
    assert set(by_norm) == {"トミカ収納", "アンパンマンシール100均"}, "裾 (imp<3) が落ちる"

    r = by_norm["トミカ収納"]
    assert r["impressions"] == 400.0 and r["clicks"] == 15.0, "表記ゆれを合流し W33 は入らない"
    # 100*2.0 + 300*6.0 = 2000 → /400 = 5.0 (単純平均なら 4.0)
    assert r["position"] == 5.0, "position は impression 加重平均"
    assert r["query"] == "トミカ収納", "表層は imp 最大の表記"
    assert meta["date"] == "2026-08-30" and meta["first_week"] == "2026-W34"


def test_output_is_readable_by_existing_consumers(tmp_path, sources):
    """**この検査が本体。** 出力を既存の3つの消費者にそのまま食わせる。"""
    src, totals = sources
    out = tmp_path / "wp_demand.jsonl"
    meta, rows = E.build(src, totals, weeks=2, min_impressions=3)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    stats = B.load_wp_rank_stats(out)
    assert "トミカ収納" in stats, "load_wp_rank_stats が norm キーで引ける"
    assert stats["トミカ収納"]["pos"] == 5.0, "1行1語なので加重平均の分母が 1 になる"
    guarded = B.normalize_key("アンパンマンシール 100均")
    assert stats[guarded]["pos"] <= 3.0 and stats[guarded]["clicks"] >= 100, \
        "ガードが外すべき語がガードの条件に入る"
    assert "" not in stats, "meta 行は query を持たないのでスキップされる"

    assert B.wp_history_last_date(out) == "2026-08-30", "鮮度は meta の date から出る"
    assert B.assert_wp_history_fresh(
        out, max_age_days=17, today=__import__("datetime").date(2026, 9, 7)
    ) == "2026-08-30", "既定の 17 日なら 2026-09-07 時点で通る"

    gaps = D.load_gsc_impressions(out, min_impressions=50)
    assert any(v["query"] == "トミカ収納" for v in gaps.values()), \
        "detect_demand_gaps も同じファイルを需要源として読める"


def test_missing_week_end_aborts(tmp_path):
    src = _write(tmp_path / "by_query.jsonl", [
        {"week": "2026-W35", "query": "語", "clicks": 1, "impressions": 50,
         "position": 3.0}])
    totals = _write(tmp_path / "totals.jsonl", [])
    with pytest.raises(SystemExit):
        E.build(src, totals, weeks=2, min_impressions=3)
