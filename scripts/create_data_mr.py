#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""データ更新 MR 作成ヘルパー (GitLab 移行 フェーズ4, 2026-07-07)。

旧 GitHub Actions 01-fetch / 07-rakuten / 34-third-party の
「Create PR + enable auto-merge」step の GitLab 版。CI job 内
(checkout 済み working tree) で以下を行う:

  1. 対象パスを `git add -f` で stage (gitignore 配下の hugo/data/ranking も対象)
  2. staged diff が無ければ何もせず正常終了 (secrets 未設定の inert run で安全に空振り)
  3. 同 prefix の古い open MR を close + source branch 削除 (stale MR の競合防止)
  4. branch を push → MR 作成 → MWPS (pipeline 成功時 auto-merge) を設定

MR パイプラインでは validate-article job が走るが、data/articles/ の記事 JSON に
変更が無ければ vacuous success → MWPS が squash merge → main push → pages 再ビルド。

env:
  GITLAB_API_TOKEN  (必須, Maintainer の project access token。git push 認証にも使う)
  CI_PROJECT_ID     (既定 84175362)
  CI_PIPELINE_ID    (branch 名サフィックス。ローカル実行時は epoch 秒)
  CI_SERVER_HOST    (既定 gitlab.com)
  CI_PROJECT_PATH   (既定 omocha/navi)
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from invoke_jules_repoless import GitLab  # noqa: E402


def sh(*args, check=True, quiet=False):
    r = subprocess.run(list(args), capture_output=quiet, text=True)
    if check and r.returncode != 0:
        # トークン入り push URL を CI ログに出さない
        tok = os.environ.get("GITLAB_API_TOKEN", "")
        cmd = " ".join(args)
        if tok:
            cmd = cmd.replace(tok, "***")
        raise SystemExit(f"command failed ({r.returncode}): {cmd}")
    return r.returncode


def close_stale_mrs(gl, prefix):
    try:
        mrs = gl.call("/merge_requests?state=opened&per_page=100")
    except urllib.error.HTTPError as e:
        print(f"warning: open MR list failed: HTTP {e.code}", file=sys.stderr)
        return
    for m in mrs:
        src = m.get("source_branch", "")
        if not src.startswith(prefix + "/"):
            continue
        iid = m["iid"]
        print(f"closing stale MR !{iid} ({src})")
        try:
            gl.call(f"/merge_requests/{iid}", data={"state_event": "close"},
                    method="PUT")
            gl.call("/repository/branches/" + urllib.parse.quote(src, safe=""),
                    method="DELETE")
        except urllib.error.HTTPError as e:
            print(f"warning: stale MR !{iid} cleanup failed: HTTP {e.code}",
                  file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True,
                    help="branch prefix (例 fetch-data → fetch-data/<pipeline_id>)")
    ap.add_argument("--title", required=True, help="commit / MR タイトル")
    ap.add_argument("--body", default="", help="MR 説明文")
    ap.add_argument("--paths", nargs="+", required=True, help="stage するパス")
    args = ap.parse_args()

    sh("git", "add", "-f", "--", *args.paths)
    if sh("git", "diff", "--cached", "--quiet", check=False) == 0:
        print("no staged changes; skipping MR creation.")
        return 0

    gl = GitLab()
    close_stale_mrs(gl, args.prefix)

    run_id = os.environ.get("CI_PIPELINE_ID") or str(int(time.time()))
    branch = f"{args.prefix}/{run_id}"
    title = f"{args.title} (run {run_id})"
    sh("git", "config", "user.name", "omocha-fetch-bot")
    sh("git", "config", "user.email", "omocha-fetch-bot@users.noreply.gitlab.com")
    sh("git", "checkout", "-b", branch)
    sh("git", "commit", "-m", title)

    host = os.environ.get("CI_SERVER_HOST", "gitlab.com")
    proj = os.environ.get("CI_PROJECT_PATH", "omocha/navi")
    token = os.environ["GITLAB_API_TOKEN"]
    push_url = f"https://oauth2:{token}@{host}/{proj}.git"
    sh("git", "push", push_url, f"HEAD:refs/heads/{branch}", quiet=True)
    print(f"pushed branch {branch}")

    mr = gl.call("/merge_requests", data={
        "source_branch": branch,
        "target_branch": "main",
        "title": title,
        "description": args.body or f"Automated data update ({args.prefix}).",
        "squash": True,
        "remove_source_branch": True,
    })
    iid = mr["iid"]
    print(f"MR created: {mr['web_url']}")
    if gl.enable_auto_merge(iid):
        print(f"auto-merge (MWPS) enabled on !{iid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
