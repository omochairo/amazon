"""scripts/check_detector_eligibility.py unit tests (#5941 / amazon-navi-brain#18).

見ているのは 1 点だけ: **「母数が無い」と「母数はあるが選別で落ちた」を取り違えないこと。**
どちらも detected == 0 になるが処方が逆 (前者は閾値、後者はサイト側) なので、
ここが崩れるとこの観察レーン自体が誤診の出どころになる。
"""
from __future__ import annotations

import json

from scripts.check_detector_eligibility import (
    NO_ELIGIBILITY_WARNING,
    collect,
    find_starved,
    main,
)


def _write(root, name, payload):
    p = root / "data" / "analytics" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _row(date, **detectors):
    return {"date": date, "detectors": detectors}


# --- eligible == 0 が 2 回続いたときだけ鳴る -------------------------------

def test_starved_when_zero_twice():
    cur = {"opportunity": {"eligible": 0, "detected": 0}}
    hist = [_row("2026-09-01", opportunity={"eligible": 0, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-08") == ["opportunity"]


def test_not_starved_on_first_zero():
    cur = {"opportunity": {"eligible": 0, "detected": 0}}
    hist = [_row("2026-09-01", opportunity={"eligible": 3, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-08") == []


def test_not_starved_without_history():
    # 導入直後に前回値が無い状態で鳴らさない (ロールアウトで誤報を出さない)。
    cur = {"opportunity": {"eligible": 0, "detected": 0}}
    assert find_starved(cur, [], "2026-09-01") == []


def test_same_date_rerun_is_not_its_own_previous():
    # 同じ日に再実行しただけで「2 回続いた」にしない。
    cur = {"opportunity": {"eligible": 0, "detected": 0}}
    hist = [_row("2026-09-01", opportunity={"eligible": 0, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-01") == []


# --- A-4 / A-5 は eligible==0 が続いても恒久的に鳴らさない (amazon-navi-brain#56/#57) ---

def test_engagement_drop_is_never_starved_even_when_eligible_zero_twice():
    # #18 で「min_pv を下げるとゲートが選別しなくなる」と確認済みで、
    # 閾値を動かさないと決めた検出器。据え置いた以上 eligible=0 が定常的に
    # 続くので、他の検出器と同じ基準で鳴らすと閾値側の合図として機能しない。
    cur = {"engagement_drop": {"eligible": 0, "detected": 0}}
    hist = [_row("2026-09-01", engagement_drop={"eligible": 0, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-08") == []


def test_orphan_pages_is_never_starved_even_when_eligible_zero_twice():
    # #56 で保留 → #57 (GA4/GSC 乖離の原因) 解消後に再検証したが、粒度を
    # 保てる PV>=20 では検出が増えず、唯一の既知真陽性も Lighthouse 自己
    # ヒットの産物と判明して無効化されたため、min_pv=50 据え置きが確定した
    # (detect_orphan_pages.py 参照)。据え置いた以上 A-4 と同じ構造。
    cur = {"orphan_pages": {"eligible": 0, "detected": 0}}
    hist = [_row("2026-09-01", orphan_pages={"eligible": 0, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-08") == []


def test_no_eligibility_warning_set_contains_engagement_drop_and_orphan_pages():
    assert NO_ELIGIBILITY_WARNING == frozenset({"engagement_drop", "orphan_pages"})


# --- 母数はあるが detected == 0 は鳴らさない (A-4 の型) --------------------

def test_eligible_present_but_nothing_detected_is_not_starved():
    cur = {"engagement_drop": {"eligible": 12, "detected": 0}}
    hist = [_row("2026-09-01", engagement_drop={"eligible": 12, "detected": 0})]
    assert find_starved(cur, hist, "2026-09-08") == [], (
        "母数があるのに鳴らすと、閾値側の問題と取り違える"
    )


# --- 欠落を 0 と混ぜない ---------------------------------------------------

def test_missing_output_is_none_not_zero(tmp_path):
    got = collect(tmp_path)
    assert got["opportunity"] is None
    assert set(got) == {
        "low_ctr", "opportunity", "cannibalization",
        "engagement_drop", "orphan_pages", "brand_suggest",
    }


def test_missing_output_does_not_warn():
    cur = {"opportunity": None}
    hist = [_row("2026-09-01", opportunity=None)]
    assert find_starved(cur, hist, "2026-09-08") == []


def test_legacy_output_without_eligible_is_none(tmp_path):
    # eligible を出していない旧版の出力を 0 と読むと、昇格直後に必ず誤報が出る。
    _write(tmp_path, "opportunity_pages.json", {"detected": []})
    assert collect(tmp_path)["opportunity"]["eligible"] is None


def test_broken_json_is_none(tmp_path):
    p = tmp_path / "data" / "analytics" / "opportunity_pages.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken", encoding="utf-8")
    assert collect(tmp_path)["opportunity"] is None


# --- candidates キーの検出器も拾う ----------------------------------------

def test_brand_suggest_uses_candidates_key(tmp_path):
    _write(tmp_path, "brand_taxonomy_suggestions.json",
           {"eligible": 7, "candidates": [{"token": "a"}, {"token": "b"}]})
    got = collect(tmp_path)["brand_suggest"]
    assert got == {"eligible": 7, "detected": 2}


# --- append の冪等性 -------------------------------------------------------

def test_append_is_idempotent_per_date(tmp_path, monkeypatch):
    _write(tmp_path, "opportunity_pages.json", {"eligible": 1, "detected": []})
    hist = tmp_path / "data" / "analytics" / "history" / "detector_eligibility.jsonl"

    def run(*extra):
        monkeypatch.setattr(
            "sys.argv",
            ["check_detector_eligibility.py", "--root", str(tmp_path),
             "--date", "2026-09-01", *extra],
        )
        assert main() == 0

    run()
    run()
    assert len(hist.read_text(encoding="utf-8").strip().splitlines()) == 1
    run("--force")
    assert len(hist.read_text(encoding="utf-8").strip().splitlines()) == 2
