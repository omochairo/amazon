"""scripts/comment_answerability_audit.py unit tests (#2995 消費側②).

外部 gh CLI 呼び出しはせず、render / parse / dedup ロジックのみを検証する。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.comment_answerability_audit import (
    AUDIT_MARKER_PREFIX,
    LOW_CTR_MARKER_PREFIX,
    find_low_ctr_issue_numbers,
    has_existing_audit_comment,
    render_comment_body,
    render_query_rows,
)


def test_render_query_rows_empty_uses_placeholder():
    out = render_query_rows([])
    assert "no query data" in out


def test_render_query_rows_formats_answered_and_missing():
    rows = [
        {"query": "何歳から", "impressions": 26, "position": 6.7, "answered": True,
         "coverage": 0.8, "missing_aspects": []},
        {"query": "のりものセット", "impressions": 16, "position": 9.0, "answered": False,
         "coverage": 0.3, "missing_aspects": ["価格帯", "対象年齢"]},
        {"query": "壊れた", "impressions": 5, "position": 12.0, "answered": None,
         "coverage": None, "missing_aspects": []},
    ]
    out = render_query_rows(rows)
    lines = out.splitlines()
    assert "何歳から" in lines[0] and "回答あり" in lines[0] and "80%" in lines[0]
    assert "のりものセット" in lines[1] and "回答不足" in lines[1] and "価格帯、対象年齢" in lines[1]
    assert "判定失敗" in lines[2] and "n/a" in lines[2]


def test_render_comment_body_includes_marker_and_disclaimer():
    page_entry = {
        "page": "https://navi.omcha.jp/products/b0fnm4y35g/",
        "page_coverage_avg": 0.55,
        "queries": [],
    }
    body = render_comment_body(page_entry, model="gemma4:26b-a4b-it-qat", source_week="2026-W28")
    marker = f"<!-- {AUDIT_MARKER_PREFIX}https://navi.omcha.jp/products/b0fnm4y35g/ -->"
    assert body.count(marker) == 2  # 先頭 dedup 用 + 末尾埋め込み
    assert "検証フェーズ" in body
    assert "自動リライトには未連動" in body
    assert "gemma4:26b-a4b-it-qat" in body
    assert "2026-W28" in body
    assert "owner の判断に委ねます" in body


def test_find_low_ctr_issue_numbers_extracts_marker_and_number():
    fake_response = {
        "items": [
            {"number": 2928, "body": f"<!-- {LOW_CTR_MARKER_PREFIX}https://navi.omcha.jp/products/b0aaa/ -->\nbody"},
            {"number": 2929, "body": f"<!-- {LOW_CTR_MARKER_PREFIX}https://navi.omcha.jp/products/b0bbb/ -->"},
            {"number": 1, "body": "unrelated issue, no marker"},
        ]
    }

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fake_response), stderr="")

    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        mapping = find_low_ctr_issue_numbers("omochairo/amazon")
    assert mapping == {
        "https://navi.omcha.jp/products/b0aaa/": 2928,
        "https://navi.omcha.jp/products/b0bbb/": 2929,
    }


def test_find_low_ctr_issue_numbers_empty():
    # 0件は search index ラグ疑いで最大 _SEARCH_MAX_ATTEMPTS 回リトライされる。
    # sleeper を no-op に差し替えてテストを高速に保つ (実 sleep しない)。
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"items": []}), stderr="")

    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        mapping = find_low_ctr_issue_numbers("repo", sleeper=lambda s: None)
    assert mapping == {}


def test_find_low_ctr_issue_numbers_retries_on_transient_zero_then_succeeds():
    # GitHub Search API のインデックス反映ラグで初回0件→再試行で見つかるケース
    # (2026-07-14 amazon-home-ops answerability-audit workflow で実際に観測:
    # 同一 run 内で data PR 作成直後にこの検索が0件を返した)。
    fake_response = {"items": [{"number": 3001, "body": f"<!-- {LOW_CTR_MARKER_PREFIX}https://x/ -->"}]}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) < 2:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"items": []}), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(fake_response), stderr="")

    sleeps = []
    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        mapping = find_low_ctr_issue_numbers("repo", sleeper=lambda s: sleeps.append(s))
    assert mapping == {"https://x/": 3001}
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_has_existing_audit_comment_true_when_marker_present():
    url = "https://navi.omcha.jp/products/b0aaa/"
    comments = [{"body": f"unrelated\n<!-- {AUDIT_MARKER_PREFIX}{url} -->\nmore"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_audit_comment("repo", 2928, url) is True


def test_has_existing_audit_comment_false_when_absent():
    comments = [{"body": "no marker here"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_audit_comment("repo", 2928, "https://navi.omcha.jp/products/x/") is False


def test_has_existing_audit_comment_false_for_different_url_marker():
    # 同じ issue に別ページの marker が付いているケース (dedup は URL 単位)
    other_url = "https://navi.omcha.jp/products/other/"
    comments = [{"body": f"<!-- {AUDIT_MARKER_PREFIX}{other_url} -->"}]

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(comments), stderr="")

    with patch("scripts.comment_answerability_audit.subprocess.run", side_effect=fake_run):
        assert has_existing_audit_comment("repo", 2928, "https://navi.omcha.jp/products/x/") is False
