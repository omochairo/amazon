"""scripts/_marker_issue.py の単体テスト (#6622)。

守りたいのは 1 行:「**検索結果は候補でしかない**」。

2026-09-06、`53-origin-failover` が裸のマーカー語で本文検索し、先頭 1 件を
無条件に採用して無関係な epic (#6602) を close した。close より怖いのは
update で、body を丸ごと差し替えるレーンでは他人の issue が中身ごと消える。
"""
from __future__ import annotations

from scripts._marker_issue import marker_comment, sole_match, verified_matches

MARKER = "delivery-freshness-monitor"
COMMENT = marker_comment(MARKER)


def test_marker_comment_is_an_html_comment():
    assert COMMENT == "<!-- delivery-freshness-monitor -->"


def test_mention_only_issue_is_rejected():
    """レーン名に言及しただけの issue を掴まない (#6622 の本体)。"""
    items = [{"number": 6602,
              "body": "寄せてはいけないもの: delivery-freshness-monitor は GH ホストのまま"}]
    assert verified_matches(items, MARKER) == []
    assert sole_match(items, MARKER) is None


def test_issue_with_marker_comment_is_accepted():
    items = [{"number": 10, "body": COMMENT + "\n\n本文"}]
    assert [i["number"] for i in verified_matches(items, MARKER)] == [10]


def test_noise_is_filtered_out_of_a_mixed_result():
    items = [
        {"number": 100, "body": "delivery-freshness-monitor について書いた記録"},
        {"number": 200, "body": COMMENT + "\n状態"},
        {"number": 300, "body": "51 の delivery-freshness-monitor を触った"},
    ]
    assert [i["number"] for i in verified_matches(items, MARKER)] == [200]


def test_result_is_ordered_by_number_not_search_rank():
    """検索の並び順に依存しない = 同じ入力なら毎回同じものを選ぶ。"""
    items = [
        {"number": 30, "body": COMMENT},
        {"number": 10, "body": COMMENT},
        {"number": 20, "body": COMMENT},
    ]
    assert [i["number"] for i in verified_matches(items, MARKER)] == [10, 20, 30]


def test_missing_body_is_not_adopted():
    """body が取れていない = 確認できていない。一致に倒さない。"""
    items = [{"number": 1}, {"number": 2, "body": None}, {"number": 3, "body": 123}]
    assert verified_matches(items, MARKER) == []


def test_empty_input():
    assert verified_matches([], MARKER) == []
    assert sole_match([], MARKER) is None


def test_sole_match_refuses_when_ambiguous():
    """複数あったら「分からない」を返す。片方を勝手に潰さない。"""
    items = [{"number": 1, "body": COMMENT}, {"number": 2, "body": COMMENT}]
    assert len(verified_matches(items, MARKER)) == 2
    assert sole_match(items, MARKER) is None


def test_different_markers_do_not_collide():
    """別レーンのマーカーを自分のものと誤認しない。"""
    other = marker_comment("asset-delivery-monitor")
    items = [{"number": 5, "body": other}]
    assert verified_matches(items, MARKER) == []


def test_marker_that_is_a_substring_of_another():
    """`foo` が `foo-bar` に含まれても、コメント形式なので混ざらない。"""
    items = [{"number": 7, "body": marker_comment("delivery-freshness-monitor-v2")}]
    assert verified_matches(items, MARKER) == []
