"""A レーンの重複防止検索 (scripts/_analytics_issue_search.py) の unit test。

以前は opener 6 本がそれぞれ 1 ページ = 100 件打ち切りの検索を手書きしており、
マーカー付き open Issue が 100 件を超えると溢れたぶんを「存在しない」と判定して
**重複した Issue を立てて**いた。gh CLI は叩かず `_run_search` を差し替える。
"""
from __future__ import annotations

from unittest.mock import patch

from scripts import _analytics_issue_search as s


def _items(n: int, start: int = 0) -> list[dict]:
    return [{"body": f"<!-- a5-orphan:/p/{i} -->"} for i in range(start, start + n)]


def _fake(pages: list[list[dict]]):
    """page 番号 (1 始まり) で引ける偽の検索。呼ばれた (page, order) を記録する。"""
    calls: list[tuple[int, str]] = []

    def run(query, page, order):
        calls.append((page, order))
        return pages[page - 1] if page - 1 < len(pages) else []

    return run, calls


def test_single_short_page_stops_immediately():
    run, calls = _fake([_items(3)])
    with patch.object(s, "_run_search", run):
        got = s.search_issues("q")
    assert len(got) == 3
    assert calls == [(1, "desc")]  # 2 ページ目は引かない


def test_paginates_past_the_first_hundred():
    # ここが本題: 100 件ちょうどで打ち切らず次ページを読む
    run, calls = _fake([_items(s.PER_PAGE), _items(s.PER_PAGE, 100), _items(7, 200)])
    with patch.object(s, "_run_search", run):
        got = s.search_issues("q")
    assert len(got) == s.PER_PAGE * 2 + 7
    assert [p for p, _ in calls] == [1, 2, 3]


def test_stops_at_the_page_cap_and_warns(caplog):
    run, calls = _fake([_items(s.PER_PAGE) for _ in range(s.MAX_PAGES + 3)])
    with patch.object(s, "_run_search", run):
        got = s.search_issues("q")
    assert len(calls) == s.MAX_PAGES
    assert len(got) == s.PER_PAGE * s.MAX_PAGES
    # 黙って打ち切らない — 重複起票のリスクが実在するので log に残す
    assert any("cap" in r.message for r in caplog.records)


def test_order_is_caller_controlled():
    # opener は desc (溢れるなら古い側を落とす)、closer は asc (期限切れを拾い切る)
    run, calls = _fake([_items(1)])
    with patch.object(s, "_run_search", run):
        s.search_issues("q", order="asc")
    assert calls == [(1, "asc")]


def test_retries_only_when_the_first_page_is_empty():
    attempts = {"n": 0}

    def run(query, page, order):
        attempts["n"] += 1
        return _items(2) if attempts["n"] >= 3 else []

    slept: list[float] = []
    with patch.object(s, "_run_search", run):
        got = s.search_issues("q", sleeper=slept.append)
    assert len(got) == 2
    assert attempts["n"] == 3
    assert slept == [s.SEARCH_RETRY_SLEEP_SECONDS] * 2


def test_gives_up_after_max_attempts_on_a_truly_empty_result():
    slept: list[float] = []
    with patch.object(s, "_run_search", lambda *a: []):
        got = s.search_issues("q", sleeper=slept.append)
    assert got == []
    assert len(slept) == s.SEARCH_MAX_ATTEMPTS - 1


def test_extract_marked_keys_handles_multiple_markers_per_body():
    items = [{"body": "<!-- a5-orphan:/a/ -->\n<!-- a5-orphan:/b/ -->"},
             {"body": "<!-- a5-orphan:/a/ -->"},
             {"body": "マーカー無し"},
             {"body": None}]
    assert s.extract_marked_keys(items, "a5-orphan:") == {"/a/", "/b/"}


def test_find_taken_keys_scopes_the_query_to_open_labelled_issues():
    seen: list[str] = []

    def fake_search(query, **kw):
        seen.append(query)
        return _items(1)

    with patch.object(s, "search_issues", fake_search):
        got = s.find_taken_keys("o/r", "a5-orphan:")
    assert got == {"/p/0"}
    assert "repo:o/r" in seen[0]
    assert "is:open" in seen[0]
    assert "label:quality label:analytics" in seen[0]
    assert 'in:body "a5-orphan:"' in seen[0]
