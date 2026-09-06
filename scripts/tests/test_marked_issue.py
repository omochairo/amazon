"""scripts/_marked_issue.py unit tests.

回帰の対象 (2026-09-02 の実害): GitHub の issue 検索は `in:body "origin-failover"`
をトークン化するため、本文に「origin failover」と書いてあるだけの無関係な issue が
ヒットする。omochairo/amazon#6415 がそれで自動 close された。検索結果の先頭を
無条件に採用してはいけない。
"""
from __future__ import annotations

from scripts._marked_issue import find_marked_issue, marker_html


def test_marker_html_wraps_in_comment():
    assert marker_html("origin-failover") == "<!-- origin-failover -->"


def test_skips_prose_mention_and_takes_the_marked_one():
    items = [
        {"number": 6415, "body": "監視 51 / 53 (origin failover) との分担を書く"},
        {"number": 900, "body": "<!-- origin-failover -->\n本文"},
    ]
    assert find_marked_issue(items, "origin-failover")["number"] == 900


def test_returns_none_when_every_hit_is_a_false_positive():
    items = [{"number": 6415, "body": "origin failover の話をしているだけ"}]
    assert find_marked_issue(items, "origin-failover") is None


def test_returns_none_for_empty_and_missing_body():
    assert find_marked_issue([], "m") is None
    assert find_marked_issue(None, "m") is None
    assert find_marked_issue([{"number": 1}], "m") is None


def test_substring_of_another_marker_does_not_match():
    """`<!-- delivery-freshness-monitor -->` を `freshness-monitor` で拾わない。"""
    items = [{"number": 1, "body": "<!-- delivery-freshness-monitor -->"}]
    assert find_marked_issue(items, "freshness-monitor") is None


def test_takes_the_first_marked_hit_when_several_match():
    items = [
        {"number": 1, "body": "no marker"},
        {"number": 2, "body": "<!-- m -->"},
        {"number": 3, "body": "<!-- m -->"},
    ]
    assert find_marked_issue(items, "m")["number"] == 2
