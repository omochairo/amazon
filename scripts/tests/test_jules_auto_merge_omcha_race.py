"""05-jules-auto-merge.yml の omcha backfill 追いコミット周りのテスト (#6788 事後)。

#6773 (案A) は記事 PR の auto-merge 直前に omcha_related.json を追いコミットする。
この追いコミット自体が push → synchronize を発火させ、同じ workflow がもう一段
再トリガされる。#6788 ではこの再トリガの中で Jules 側の別コミットが追いコミットを
削除し、かつ native auto-merge がその「削除された状態」の head を先に squash した
(#6788 事後調査)。ここでは再発防止の 2 点を検証する:

  1. 「Determine omcha_related backfill targets」step: HEAD コミットが自分
     (omochairo-fetch-bot[bot]) の追いコミットなら fetch/push を skip する
     (無駄な再トリガの連鎖を1段で止める)。
  2. 「Enable native auto-merge (squash)」step: `gh pr view` で都度取り直した
     現在の head sha を `--match-head-commit` に渡す (この run が確認した
     head 以外への squash を `gh pr merge` 自身に検出させる)。失敗時は
     `::warning::` を必ず出す (握り潰さない)。

**ロジックをコピーせず、ワークフロー YAML から run スクリプトを実際に抜き出して
実行する** (test_jules_auto_merge_scope.py と同じ方針)。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "05-jules-auto-merge.yml"
ARTICLE = "data/articles/2026-09-08-B0DPHB7DMT.json"
BOT_EMAIL = "omochairo-fetch-bot[bot]@users.noreply.github.com"


def _bash() -> str | None:
    for candidate in ("/bin/bash", "bash"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


BASH = _bash()


def _load_step_script(step_id: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["enable-auto-merge"]["steps"]:
        if step.get("id") == step_id:
            return step["run"]
    raise AssertionError(f"id={step_id} のステップが 05-jules-auto-merge.yml に見つからない")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class OmchaBackfillTargetTests(unittest.TestCase):
    """『Determine omcha_related backfill targets』(id: omcha) の判定。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_step_script("omcha")

    def _run(self, *, head_email: str, add_cache: bool) -> dict[str, str]:
        """base に記事 JSON を追加した commit を積み、omcha スクリプトの出力を返す。

        head_email: HEAD コミットの author/committer email。
        add_cache: True なら HEAD コミットで同時に omcha_related.json も作る
                   (=既にキャッシュ済みのケースを模す)。
        """
        tmp = tempfile.mkdtemp()
        try:
            setup = (
                "git init -q . && git config user.email base@example.com "
                "&& git config user.name base "
                "&& mkdir -p data/articles data/raw/per_asin/B0DPHB7DMT "
                "&& echo base > README.md && git add -A && git commit -qm base"
            )
            subprocess.run([BASH, "-lc", setup], cwd=tmp, capture_output=True, text=True, check=True)
            base = subprocess.run([BASH, "-lc", "git rev-parse HEAD"], cwd=tmp,
                                  capture_output=True, text=True, check=True).stdout.strip()

            mutate = f'echo "{{}}" > {ARTICLE}'
            if add_cache:
                mutate += ' && echo "{}" > data/raw/per_asin/B0DPHB7DMT/omcha_related.json'
            head_cmd = (
                f"{mutate} && git add -A && "
                f'GIT_AUTHOR_EMAIL="{head_email}" GIT_COMMITTER_EMAIL="{head_email}" '
                f'GIT_AUTHOR_NAME=x GIT_COMMITTER_NAME=x git commit -qm change'
            )
            subprocess.run([BASH, "-lc", head_cmd], cwd=tmp, capture_output=True, text=True, check=True)
            head = subprocess.run([BASH, "-lc", "git rev-parse HEAD"], cwd=tmp,
                                  capture_output=True, text=True, check=True).stdout.strip()

            out_path = os.path.join(tmp, "gh_output")
            open(out_path, "w", encoding="utf-8").close()
            script_path = os.path.join(tmp, "omcha.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.script)

            env = dict(os.environ, BASE_SHA=base, HEAD_SHA=head,
                       GITHUB_OUTPUT=out_path.replace("\\", "/"))
            proc = subprocess.run([BASH, script_path], cwd=tmp, capture_output=True,
                                  text=True, env=env)
            self.assertEqual(proc.returncode, 0,
                             f"omcha スクリプトが異常終了した\n{proc.stdout}\n{proc.stderr}")

            outputs: dict[str, str] = {}
            for line in open(out_path, encoding="utf-8"):
                if "=" in line and "<<" not in line:
                    key, _, value = line.strip().partition("=")
                    if key == "asins":
                        outputs[key] = value
            return outputs

        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_asin_without_cache_is_targeted(self):
        """通常のケース: 人間/Jules のコミットで新規記事が追加され、キャッシュが無い。"""
        out = self._run(head_email="jules@example.com", add_cache=False)
        self.assertEqual(out.get("asins"), "B0DPHB7DMT")

    def test_cached_asin_is_skipped(self):
        """キャッシュが既に存在する ASIN は対象にしない (従来どおりの回帰確認)。"""
        out = self._run(head_email="jules@example.com", add_cache=True)
        self.assertEqual(out.get("asins", ""), "")

    def test_bot_own_commit_head_is_skipped(self):
        """#6788 事後: HEAD が自分 (omochairo-fetch-bot[bot]) の追いコミットなら
        キャッシュの有無に関わらず fetch/push を skip する。

        再トリガされた run が「自分の直前の push」を見ているだけなので、
        ここで再度 fetch_omcha_related.py を叩く意味が無い
        (二重 API 呼び出し・二重 push の温床になっていた)。
        """
        out = self._run(head_email=BOT_EMAIL, add_cache=False)
        self.assertEqual(out.get("asins", ""), "")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class AutoMergeMatchHeadCommitTests(unittest.TestCase):
    """『Enable native auto-merge (squash)』(id: auto-merge) の headSha 固定。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_step_script("auto-merge")

    def _fake_gh(self, tmp: str, *, head_sha: str, merge_exit: int) -> str:
        """gh pr view / gh pr merge を偽装する gh を書き、その bin dir を返す。

        呼び出された引数はそのまま /tmp/gh_calls.log に追記するので、
        テスト側で `--match-head-commit <sha>` が実際に渡ったかを確認できる。
        """
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        gh_path = os.path.join(bindir, "gh")
        calls_log = os.path.join(tmp, "gh_calls.log")
        script = f"""#!/usr/bin/env bash
echo "$@" >> "{calls_log}"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo "{head_sha}"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  exit {merge_exit}
fi
echo "unexpected gh invocation: $@" >&2
exit 99
"""
        with open(gh_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        os.chmod(gh_path, os.stat(gh_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return bindir, calls_log

    def _run(self, *, head_sha: str, merge_exit: int) -> tuple[subprocess.CompletedProcess, str]:
        tmp = tempfile.mkdtemp()
        try:
            bindir, calls_log = self._fake_gh(tmp, head_sha=head_sha, merge_exit=merge_exit)
            script_path = os.path.join(tmp, "auto-merge.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.script)

            env = dict(
                os.environ,
                GH_TOKEN="dummy",
                PR_URL="https://github.com/omochairo/amazon/pull/6788",
                PR_NUMBER="6788",
                REPO="omochairo/amazon",
                PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
            )
            proc = subprocess.run([BASH, script_path], cwd=tmp, capture_output=True,
                                  text=True, env=env)
            calls = open(calls_log, encoding="utf-8").read() if os.path.exists(calls_log) else ""
            return proc, calls
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pins_match_head_commit_to_live_head_sha(self):
        """`gh pr view` で取り直した現在の head sha を --match-head-commit に渡す。"""
        proc, calls = self._run(head_sha="deadbeef123", merge_exit=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--match-head-commit deadbeef123", calls)
        self.assertIn("pinned to deadbeef123", proc.stdout)

    def test_match_head_commit_failure_is_logged_not_silent(self):
        """gh pr merge が (head 不一致等で) 失敗しても step 自体は落とさず、
        `::warning::` で理由を残す (#6788: 握り潰されて無言で保護が外れるのを防ぐ)。
        """
        proc, calls = self._run(head_sha="cafef00d", merge_exit=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--match-head-commit cafef00d", calls)
        self.assertIn("::warning::", proc.stdout)
        self.assertIn("match-head-commit", proc.stdout)


if __name__ == "__main__":
    unittest.main()
