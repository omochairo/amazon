"""scripts/detect_query_intent.py unit tests (A-7, epic #1356 / #1980)。

主眼は ledger の持ち越し (brain#47/#46/#45)。週次窓に入らなかったページを
「意図が変わった」と読んで cta_layout を剥がす挙動が退行しないように固める。
"""
from __future__ import annotations

import json

from scripts.detect_query_intent import classify_query, detect

PAGE = "https://navi.omcha.jp/products/b010cqeucu/"
OTHER = "https://navi.omcha.jp/english-toys/"


def _combo(page, query, impressions):
    return {"page": page, "query": query, "impressions": impressions,
            "clicks": 0, "position": 9.0}


def _gsc(by_combo, end="2026-09-17"):
    return {"range": {"start": "2026-09-10", "end": end}, "by_combo": by_combo}


def _informational_page(page, impressions=80, end="2026-09-17"):
    """min_impressions / min_dominant_share を跨ぐ情報収集ページ 1 枚の GSC。"""
    return _gsc([_combo(page, "ボーネルンド ラッパ", impressions)], end=end)


def test_classify_query_precedence():
    # navigational を先に見るのは「公式」等が最も具体的だから
    assert classify_query("ボーネルンド 公式 おすすめ") == "navigational"
    assert classify_query("知育玩具 英語 ランキング") == "commercial"
    assert classify_query("ボーネルンド ラッパ 洗い方") == "informational"


def test_detected_page_is_marked_fresh_with_provenance():
    result = detect(_informational_page(PAGE))
    (row,) = result["detected"]
    assert row["dominant_intent"] == "informational"
    assert row["carried_forward"] is False
    assert row["weeks_since_confirmed"] == 0
    # #6812 が前後比較の基準日として要る
    assert row["first_detected"] == "2026-09-17"
    assert row["last_confirmed"] == "2026-09-17"


def test_page_missing_this_week_is_carried_forward_not_dropped():
    """欠測で cta_layout を剥がさない (amazon#7953 と同じ型の退行を防ぐ)。"""
    week1 = detect(_informational_page(PAGE, end="2026-09-10"))
    week2 = detect(_gsc([], end="2026-09-17"), previous=week1)

    (row,) = week2["detected"]
    assert row["page"] == PAGE
    assert row["dominant_intent"] == "informational"
    assert row["carried_forward"] is True
    assert row["weeks_since_confirmed"] == 1
    assert row["first_detected"] == "2026-09-10"
    # 再確認できていないので last_confirmed は動かさない
    assert row["last_confirmed"] == "2026-09-10"


def test_carry_forward_expires_after_sticky_weeks():
    ledger = detect(_informational_page(PAGE, end="2026-09-10"))
    for end in ("2026-09-17", "2026-09-24", "2026-10-01"):
        ledger = detect(_gsc([], end=end), previous=ledger, sticky_weeks=3)
    assert len(ledger["detected"]) == 1
    assert ledger["detected"][0]["weeks_since_confirmed"] == 3

    ledger = detect(_gsc([], end="2026-10-08"), previous=ledger, sticky_weeks=3)
    assert ledger["detected"] == []


def test_sticky_weeks_zero_restores_snapshot_behaviour():
    week1 = detect(_informational_page(PAGE, end="2026-09-10"))
    week2 = detect(_gsc([], end="2026-09-17"), previous=week1, sticky_weeks=0)
    assert week2["detected"] == []


def test_new_observation_overrides_carried_intent():
    """持ち越しは反証まで。今週分類できたら新しい意図で上書きする。"""
    week1 = detect(_informational_page(PAGE, end="2026-09-10"))
    week2 = detect(
        _gsc([_combo(PAGE, "ボーネルンド ラッパ おすすめ ランキング", 80)]),
        previous=week1,
    )
    (row,) = week2["detected"]
    assert row["dominant_intent"] == "commercial"
    assert row["carried_forward"] is False
    assert row["weeks_since_confirmed"] == 0
    assert row["first_detected"] == "2026-09-10"  # 起点は最初の検出日のまま
    assert row["last_confirmed"] == "2026-09-17"


def test_rerunning_the_same_week_does_not_age_the_ledger():
    week1 = detect(_informational_page(PAGE, end="2026-09-10"))
    dropped = detect(_gsc([], end="2026-09-17"), previous=week1)
    again = detect(_gsc([], end="2026-09-17"), previous=dropped)
    assert again["detected"][0]["weeks_since_confirmed"] == 1


def test_max_results_truncates_fresh_only():
    """今週の検出が多い週に、持ち越し分が上限で押し出されないこと。"""
    week1 = detect(_informational_page(OTHER, end="2026-09-10"))
    fresh = [_combo(f"https://navi.omcha.jp/products/p{i}/", "ラッパ とは", 80)
             for i in range(3)]
    week2 = detect(_gsc(fresh), previous=week1, max_results=2)

    pages = [row["page"] for row in week2["detected"]]
    assert len(pages) == 3  # fresh 2 + carried 1
    assert OTHER in pages
    assert week2["detected"][-1]["carried_forward"] is True


def test_unreadable_previous_is_treated_as_absent(tmp_path):
    from scripts.detect_query_intent import _load_previous

    missing = tmp_path / "nope.json"
    assert _load_previous(missing) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _load_previous(broken) == {}

    wrong_type = tmp_path / "list.json"
    wrong_type.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert _load_previous(wrong_type) == {}


def test_params_record_sticky_weeks():
    result = detect(_gsc([]), sticky_weeks=4)
    assert result["params"]["sticky_weeks"] == 4
