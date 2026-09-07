"""サイト別スコアラ (omcha-ops#97 P3) の検査。

**守りたいのは5つ。**

1. **未測定を 0 として並べない。** 「測っていない」を「需要が無い」と扱うと、
   実際には需要のある語が最下位に沈んで永久に候補に上がらない
   (実測: `児童 生徒 違い` は外部 0 / GSC 1,750 impr)。
2. **需要を足さない。** WP impressions と外部 SV は同じ需要を別経路で測ったもの。
3. **omcha レーンがリライト候補を出さない。** 選定は rewrite_radar に一本化すると
   2026-08-21 に決めてある。ここで再実装すると黙って巻き戻す。
4. **host crowding ガードが navi の rank guard と同じ条件で効く。**
5. **供給 probe に無い語を「商品が無い」と読まない。** 未調査と 0 件は違う。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import kw_ledger as L  # noqa: E402
import kw_score as S  # noqa: E402


def _row(keyword, sv=None, measured=True, monthly=None, unknown=False, sd=None):
    return {
        "keyword": keyword, "norm": L.normalize_key(keyword),
        "loc_id": 2392, "language": "ja",
        "sv": sv, "sd": sd,
        "monthly_searches": monthly or ([{"month": "2026-08", "volume": sv}]
                                        if measured else []),
        "measured": measured,
        **({"measured_unknown": True} if unknown else {}),
        "fetched_at": "2026-09-07",
    }


def _wp(keyword, impressions=0.0, clicks=0.0, position=50.0):
    return {"query": keyword, "norm": L.normalize_key(keyword),
            "impressions": impressions, "clicks": clicks, "position": position}


def test_split_by_measurement_keeps_three_buckets():
    rows = [_row("測れた", 100), _row("未測定", 0, measured=False),
            _row("判定してない", 500, unknown=True)]
    measured, unmeasured, unknown = S.split_by_measurement(rows)
    assert [r["keyword"] for r in measured] == ["測れた"]
    assert [r["keyword"] for r in unmeasured] == ["未測定"]
    assert [r["keyword"] for r in unknown] == ["判定してない"]


def test_unmeasured_is_never_scored():
    """未測定は 0 ではない。スコアの土俵に上げない。"""
    rows = [_row("未測定", 0, measured=False)]
    measured, unmeasured, _ = S.split_by_measurement(rows)
    scored, _ = S.score_navi(measured, {}, {}, 3.0, 100.0)
    assert scored == [] and len(unmeasured) == 1


def test_demand_is_maxed_not_summed():
    r = _row("語", 1000)
    wp = {"impressions": 3000.0}
    assert S.demand_of(r, wp) == (3000.0, "wp_gsc")
    assert S.demand_of(r, {"impressions": 10.0}) == (1000.0, "external")
    # 足していたら 4000 になる
    assert S.demand_of(r, wp)[0] != 4000.0


def test_navi_drops_wp_owned_words():
    rows = [_row("WPの語", 5000), _row("空いてる語", 4000)]
    wp = {L.normalize_key("WPの語"): _wp("WPの語", impressions=9000.0,
                                        clicks=400.0, position=1.4)}
    scored, dropped = S.score_navi(rows, wp, {}, 3.0, 100.0)
    assert [r["keyword"] for r in scored] == ["空いてる語"]
    assert dropped["wp_owned"] == 1


def test_navi_guard_needs_both_conditions():
    """pos だけ / clicks だけでは落とさない (navi の rank guard と同じ AND)。"""
    rows = [_row("上位だが薄い", 100), _row("多いが下位", 100)]
    wp = {
        L.normalize_key("上位だが薄い"): _wp("上位だが薄い", 50.0, clicks=5.0,
                                       position=1.2),
        L.normalize_key("多いが下位"): _wp("多いが下位", 5000.0, clicks=900.0,
                                      position=12.0),
    }
    scored, dropped = S.score_navi(rows, wp, {}, 3.0, 100.0)
    assert len(scored) == 2 and not dropped


def test_navi_supply_gate_separates_zero_from_unknown():
    rows = [_row("商品なし", 5000), _row("未調査", 4000), _row("商品あり", 3000)]
    supply = {L.normalize_key("商品なし"): 0, L.normalize_key("商品あり"): 10}
    scored, dropped = S.score_navi(rows, supply=supply, wp={},
                                   guard_pos_max=3.0, guard_min_clicks=100.0)
    got = {r["keyword"]: r["supply_hits"] for r in scored}
    assert "商品なし" not in got and dropped["no_supply"] == 1
    assert got["未調査"] == "unknown", "probe に無いのは未調査であって 0 件ではない"
    assert got["商品あり"] == 10


def test_omcha_lane_excludes_words_already_ranking():
    """リライト候補は rewrite_radar の領分。ここでは出さない。"""
    rows = [_row("記事がある語", 900), _row("記事が無い語", 800)]
    wp = {L.normalize_key("記事がある語"): _wp("記事がある語", impressions=1750.0,
                                         clicks=30.0, position=10.2)}
    scored, dropped = S.score_omcha(rows, wp, present_min_impressions=10.0)
    assert [r["keyword"] for r in scored] == ["記事が無い語"]
    assert dropped["already_ranking"] == 1


def test_omcha_lane_ignores_impression_noise():
    """1〜2 imp の裾で新規候補を落とさない。"""
    rows = [_row("裾だけ出ている語", 800)]
    wp = {L.normalize_key("裾だけ出ている語"): _wp("裾だけ出ている語",
                                          impressions=3.0, position=48.0)}
    scored, _ = S.score_omcha(rows, wp, present_min_impressions=10.0)
    assert [r["keyword"] for r in scored] == ["裾だけ出ている語"]


def test_season_needs_enough_months():
    assert S.season([{"month": "2026-08", "volume": 100}]) is None
    monthly = [{"month": "2026-%02d" % m, "volume": v} for m, v in
               zip(range(1, 13), [1300, 1300, 1400, 1500, 1600, 1700,
                                  1900, 1900, 1900, 1500, 1400, 1300])]
    s = S.season(monthly)
    assert s["peak_month"] in ("2026-07", "2026-08", "2026-09")
    assert s["ratio"] == pytest.approx(1900 / 1300, abs=0.01)  # 小数2桁に丸める


def test_load_wp_skips_meta_line(tmp_path):
    p = tmp_path / "wp_demand.jsonl"
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"kind": "wp_query_window", "date": "2026-08-30"}) + "\n")
        f.write(json.dumps(_wp("語", 100.0, 5.0, 8.0), ensure_ascii=False) + "\n")
    wp = S.load_wp(p)
    assert list(wp) == [L.normalize_key("語")]


def test_cli_reports_buckets_without_scoring_them(tmp_path, capsys):
    ext = tmp_path / "external.jsonl"
    with ext.open("w", encoding="utf-8", newline="\n") as f:
        for r in [_row("測れた語", 500), _row("未測定の語", 0, measured=False),
                  _row("判定してない語", 900, unknown=True)]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    S.main(["--external", str(ext), "--wp-demand", str(tmp_path / "none.jsonl"),
            "navi", "--supply-probe", str(tmp_path / "none.json")])
    out = capsys.readouterr().out
    assert "未測定 1 語 / 判定していない 1 語" in out
    assert "測れた語" in out and "未測定の語" not in out
    assert "WP ブリッジが空" in out, "ガードが効いていないことを黙らない"


def test_filter_blocks_is_substring_and_multi():
    rows = [{"block": "navi-competitor-2026-08"}, {"block": "block1-日光"},
            {"block": ""}]
    assert len(S.filter_blocks(rows, "navi-competitor")) == 1
    assert len(S.filter_blocks(rows, "navi-competitor,block1")) == 2
    assert len(S.filter_blocks(rows, None)) == 3, "指定が無ければ全件"
