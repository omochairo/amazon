"""gh_rest.py — gh CLI 経由の GitHub REST API 呼び出し共通ヘルパー。

背景 (2026-07-26 amazon-home-ops run 30220976171 で判明・Refs #3300 #4058 #3203):
  GitHub App installation token (`actions/create-github-app-token@v3`,
  owner=omochairo / repositories=amazon) を使う自動化で、`gh issue comment` が
  exit 1 で失敗する。一方 `gh api` 経由の REST API (issue コメントの読み取り/
  投稿を含む) は同じ token で正常に動く。

  証拠: scripts/comment_uniqueness_audit.py の `has_existing_week_comment()`
  (`gh api -X GET repos/.../issues/3300/comments`) は成功する一方、直後の
  `post_comment()` (`gh issue comment 3300 ...`) は CalledProcessError で失敗した。
  同 App token で `gh pr create` / `gh issue create` は成功している — これらは
  もともと REST を使うため影響を受けない (create 系はこの理由で無変更)。

  結論: gh CLI の **GraphQL 経路 (addComment mutation) がこの App token で
  通らない**。issue への書き込みは REST に統一する。同型の修正は lighthouse
  レーン (#4057 / amazon-home-ops#45) で `gh issue create` → REST 置換として
  先行実施済み。

既知の罠:
  `gh api URL -f key=value` は自動的に POST に切り替わるが、本文に改行・
  マルチバイト・`=` を含む長文コメントを `-f body=...` で渡すのは argv 長・
  エスケープの両面で危険。必ず `--input -` + stdin JSON で渡すこと
  (post_issue_comment 参照)。

呼び出し元 (2026-07): scripts/comment_uniqueness_audit.py,
scripts/comment_answerability_audit.py, scripts/open_article_feedback_issue.py
"""
from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def run_gh(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    """``gh`` CLI を実行する共通ラッパー。

    失敗 (非 0 exit) 時は必ず ``logger.error`` で stderr を出してから re-raise
    する。これまでの各スクリプトは ``capture_output=True`` + ``check=True`` の
    まま ``subprocess.run`` を直接呼んでいたため、``CalledProcessError`` の
    stderr がログに一切残らず失敗の真因が追えなかった (2026-07-26 発覚)。
    """
    try:
        return subprocess.run(
            ["gh", *args],
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        logger.error(
            "gh %s failed (rc=%s): %s",
            " ".join(args), e.returncode, (e.stderr or "").strip(),
        )
        raise


def post_issue_comment(repo: str, issue_number: int, body: str) -> str:
    """Issue にコメントを REST 経由で投稿する (`gh issue comment` の GraphQL 経路を回避)。

    本文は stdin から JSON で渡す — argv 経由の ``-f body=...`` は改行/
    マルチバイト/長文で壊れやすいため使わない。

    戻り値は投稿されたコメントの ``html_url`` (旧 `gh issue comment` の stdout
    と同じくログ表示用)。レスポンスの形が想定外ならログ用に生の stdout を返す。
    """
    res = run_gh(
        ["api", "--method", "POST",
         f"repos/{repo}/issues/{issue_number}/comments", "--input", "-"],
        input_text=json.dumps({"body": body}),
    )
    stdout = res.stdout.strip()
    try:
        html_url = json.loads(stdout).get("html_url")
    except (json.JSONDecodeError, AttributeError):
        html_url = None
    return html_url or stdout
