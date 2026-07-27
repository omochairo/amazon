"""scripts/gh_rest.py unit tests.

背景 (Refs #3300 #4058 #3203): GitHub App installation token では
`gh issue comment` (GraphQL addComment) が失敗するため、issue コメント投稿は
REST (`gh api --method POST .../comments`) に統一した。本モジュールはその
共通ヘルパーであり、以下を検証する:
  - REST エンドポイントと --method POST が正しく組み立てられること
  - 本文は argv ではなく stdin JSON (--input -) で渡すこと
  - 失敗時に stderr が logger.error でログに出ること (握り潰し禁止)
"""
from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import patch

import pytest

from scripts import gh_rest


def test_run_gh_prefixes_gh_and_returns_completed_process():
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "gh"
        assert cmd[1:] == ["api", "-X", "GET", "repos/owner/repo/issues/1/comments"]
        return subprocess.CompletedProcess(cmd, 0, stdout='[]', stderr="")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        res = gh_rest.run_gh(["api", "-X", "GET", "repos/owner/repo/issues/1/comments"])
    assert res.stdout == "[]"


def test_run_gh_logs_stderr_on_failure(caplog):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="HTTP 403: Resource not accessible by integration"
        )

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        with caplog.at_level(logging.ERROR, logger="scripts.gh_rest"):
            with pytest.raises(subprocess.CalledProcessError):
                gh_rest.run_gh(["issue", "comment", "1", "-R", "owner/repo", "--body", "x"])

    # 修正前は capture_output=True + check=True のまま呼んでいたため、失敗時に
    # stderr が一切ログに残らなかった (2026-07-26 発覚)。stderr の内容がログに
    # 出ていることを検証する。
    assert any(
        "Resource not accessible by integration" in record.message
        for record in caplog.records
    )


def test_post_issue_comment_uses_rest_post_endpoint_and_stdin_json():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"html_url": "https://github.com/owner/repo/issues/1#issuecomment-1"}),
            stderr="",
        )

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        url = gh_rest.post_issue_comment("owner/repo", 1, "本文\n改行あり = テスト")

    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "api", "--method"]
    assert "POST" in cmd
    assert "repos/owner/repo/issues/1/comments" in cmd
    assert "--input" in cmd and cmd[cmd.index("--input") + 1] == "-"
    # 本文は argv ではなく stdin JSON で渡す (改行/マルチバイト/`=` を含む長文が
    # -f body=... では壊れるため)
    assert not any("本文" in part for part in cmd)
    assert json.loads(captured["input"]) == {"body": "本文\n改行あり = テスト"}
    assert url == "https://github.com/owner/repo/issues/1#issuecomment-1"


def test_post_issue_comment_falls_back_to_raw_stdout_when_not_json():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        result = gh_rest.post_issue_comment("owner/repo", 1, "body")
    assert result == "not json"


def test_post_issue_comment_propagates_failure_and_logs_stderr(caplog):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")

    with patch("scripts.gh_rest.subprocess.run", side_effect=fake_run):
        with caplog.at_level(logging.ERROR, logger="scripts.gh_rest"):
            with pytest.raises(subprocess.CalledProcessError):
                gh_rest.post_issue_comment("owner/repo", 1, "body")
    assert any("boom" in record.message for record in caplog.records)
