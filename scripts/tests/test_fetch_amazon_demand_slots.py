"""需要側キーワードの優先枠 (#2686) の検査。"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_amazon as F  # noqa: E402


def _iso(days_ago: float) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _kw_file(tmp_path, keywords, *, age_days: float = 0.0, generated_at: str | None = "auto"):
    p = tmp_path / "demand_keywords.json"
    payload = {"keywords": [{"keyword": k, "wp_impressions": i}
                            for i, k in enumerate(keywords)]}
    if generated_at == "auto":
        payload["generated_at"] = _iso(age_days)
    elif generated_at is not None:
        payload["generated_at"] = generated_at
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_top_n_keywords_are_taken_in_file_order(tmp_path):
    """ファイルは需要 imp 降順で並んでいるので、上から取れば需要順になる。"""
    p = _kw_file(tmp_path, ["トミカ収納", "スクイーズ", "知育ボックス"])
    assert F.load_demand_keywords(p, 2) == ["トミカ収納", "スクイーズ"]


def test_slots_larger_than_file_returns_all(tmp_path):
    p = _kw_file(tmp_path, ["トミカ収納"])
    assert F.load_demand_keywords(p, 99) == ["トミカ収納"]


@pytest.mark.parametrize("slots", [0, -1])
def test_zero_or_negative_slots_disables_the_feature(tmp_path, slots):
    """既定 0 = 従来動作。コードをマージしても配線するまで何も変わらない。"""
    p = _kw_file(tmp_path, ["トミカ収納"])
    assert F.load_demand_keywords(p, slots) == []


def test_missing_file_falls_back_to_supply_side_only(tmp_path):
    assert F.load_demand_keywords(str(tmp_path / "nope.json"), 20) == []


def test_broken_json_falls_back_without_raising(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert F.load_demand_keywords(str(p), 20) == []


def test_empty_path_is_safe():
    assert F.load_demand_keywords("", 20) == []


def test_malformed_entries_are_skipped(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"generated_at": _iso(0), "keywords": [
        {"keyword": "トミカ収納"}, {"nope": 1}, "文字列", {"keyword": "   "},
        {"keyword": "スクイーズ"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert F.load_demand_keywords(str(p), 10) == ["トミカ収納", "スクイーズ"]


def test_real_demand_keywords_file_is_loadable():
    """実ファイルが load できること (形が変わったら気づく)。"""
    real = ROOT.parent / "data" / "demand_keywords.json"
    if not real.exists():  # pragma: no cover
        pytest.skip("demand_keywords.json not present")
    # 形の検査なので鮮度ガードは切る。実ファイルが古いかどうかは
    # 別レーン (55-demand-keywords-refresh) の責務で、ここで CI を落とすと
    # 「生成が止まっている」が「テストが壊れた」に化ける (brain#34)。
    kws = F.load_demand_keywords(str(real), 20, max_age_days=0)
    assert len(kws) == 20
    assert all(isinstance(k, str) and k for k in kws)
    # 供給ゲートを通過済みのファイルなので、非販売と分かっている語は入らない
    assert not any("メロジョイ" in k for k in kws)


# --- 鮮度ガード (brain#34) ---------------------------------------------------
#
# 2026-08-12〜09-07 の 34 日間、このレーンは 08-10 のスナップショットを 20 枠に
# 使い続けていた。生成側 (build_demand_keywords.py) の fail-closed ガードは
# 生成を止めた時点で一緒に迂回され、消費側からは何も見えなかった。


def test_fresh_file_is_used(tmp_path):
    p = _kw_file(tmp_path, ["トミカ収納", "スクイーズ"], age_days=3)
    assert F.load_demand_keywords(p, 2) == ["トミカ収納", "スクイーズ"]


def test_stale_file_falls_back_to_supply_side(tmp_path):
    """古いファイルは需要枠を 0 にする。job は落とさない (供給側で回り続ける)。"""
    p = _kw_file(tmp_path, ["トミカ収納"], age_days=40)
    assert F.load_demand_keywords(p, 20) == []


def test_stale_file_warns_on_stdout(tmp_path, capsys):
    """Actions の annotation に出ること。ログだけだと run 一覧から見えない。"""
    p = _kw_file(tmp_path, ["トミカ収納"], age_days=40)
    F.load_demand_keywords(p, 20)
    assert "::warning::" in capsys.readouterr().out


def test_boundary_is_inclusive(tmp_path):
    """上限ちょうどは通す (17d 上限で 17.0d のファイルを弾かない)。"""
    p = _kw_file(tmp_path, ["トミカ収納"], age_days=16.9)
    assert F.load_demand_keywords(p, 1, max_age_days=17) == ["トミカ収納"]


def test_missing_generated_at_is_treated_as_stale(tmp_path):
    """年齢が分からないものを「新しい」に倒すと、生成が壊れた瞬間にガードが死ぬ。"""
    p = _kw_file(tmp_path, ["トミカ収納"], generated_at=None)
    assert F.load_demand_keywords(p, 20) == []


def test_unparseable_generated_at_is_treated_as_stale(tmp_path):
    p = _kw_file(tmp_path, ["トミカ収納"], generated_at="not-a-date")
    assert F.load_demand_keywords(p, 20) == []


def test_naive_generated_at_is_read_as_utc(tmp_path):
    """タイムゾーン無しの生成時刻でも例外にしない (UTC とみなす)。"""
    naive = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p = _kw_file(tmp_path, ["トミカ収納"], generated_at=naive)
    assert F.load_demand_keywords(p, 1) == ["トミカ収納"]


def test_guard_can_be_disabled(tmp_path):
    p = _kw_file(tmp_path, ["トミカ収納"], age_days=400)
    assert F.load_demand_keywords(p, 1, max_age_days=0) == ["トミカ収納"]
