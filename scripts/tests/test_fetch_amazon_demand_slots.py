"""需要側キーワードの優先枠 (#2686) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_amazon as F  # noqa: E402


def _kw_file(tmp_path, keywords):
    p = tmp_path / "demand_keywords.json"
    p.write_text(json.dumps({"keywords": [{"keyword": k, "wp_impressions": i}
                                          for i, k in enumerate(keywords)]},
                            ensure_ascii=False), encoding="utf-8")
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
    p.write_text(json.dumps({"keywords": [
        {"keyword": "トミカ収納"}, {"nope": 1}, "文字列", {"keyword": "   "},
        {"keyword": "スクイーズ"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert F.load_demand_keywords(str(p), 10) == ["トミカ収納", "スクイーズ"]


def test_real_demand_keywords_file_is_loadable():
    """実ファイルが load できること (形が変わったら気づく)。"""
    real = ROOT.parent / "data" / "demand_keywords.json"
    if not real.exists():  # pragma: no cover
        pytest.skip("demand_keywords.json not present")
    kws = F.load_demand_keywords(str(real), 20)
    assert len(kws) == 20
    assert all(isinstance(k, str) and k for k in kws)
    # 供給ゲートを通過済みのファイルなので、非販売と分かっている語は入らない
    assert not any("メロジョイ" in k for k in kws)
