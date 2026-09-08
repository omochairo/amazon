"""ASIN 出自台帳 (#4964 観察項目3) 用の検索語レーン判定の検査。"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_amazon as F  # noqa: E402


def test_user_keyword_is_lane_user():
    assert F.classify_keyword_lane("トミカ収納", ["トミカ収納"], []) == "user"


def test_demand_keyword_is_lane_demand():
    assert F.classify_keyword_lane("トミカ収納", [], ["トミカ収納"]) == "demand"


def test_neither_is_lane_supply_random():
    assert F.classify_keyword_lane("トミカ収納", [], []) == "supply-random"


def test_user_wins_when_keyword_is_in_both_user_and_demand():
    """user 指定語が demand 語彙と重なっても、明示指定の事実の方が確度が高いので user 優先。"""
    assert F.classify_keyword_lane("トミカ収納", ["トミカ収納"], ["トミカ収納"]) == "user"
