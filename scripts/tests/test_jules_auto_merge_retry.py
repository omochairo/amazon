"""05-jules-auto-merge.yml の auto-merge 再試行ロジックのテスト (#7225)。

`pull_request` イベント発火直後は required checks (unit-tests / validate) がまだ
完走しておらず PR が GitHub 上 "unstable" のことがある。この状態で
`gh pr merge --auto --match-head-commit` を叩くと GraphQL が
"Pull request is in unstable status" で失敗する。旧実装はこの失敗を
`::warning::` に流すだけで、次に head へ追いコミットが無い限り (= synchronize
イベントが再発火しない限り) 二度と再試行しなかった (PR #7057 で発覚、2 日間滞留)。

「Wait for merge and dispatch GitLab mirror」(id: wait-for-merge) は既に PR が
MERGED になるまで 6 分 (15s × 24) ポーリングしている。ここでは auto-merge が
未登録 (autoMergeRequest == null) かつ mergeStateStatus が CLEAN まで進んでいたら
その場で `gh pr merge --auto` を再試行することを検証する
(#6788 の `--match-head-commit` 安全策は維持されたまま)。

**ロジックをコピーせず、ワークフロー YAML から run スクリプトを実際に抜き出して
実行する** (test_jules_auto_merge_scope.py / test_jules_auto_merge_omcha_race.py
と同じ方針)。
"""
from __future__ import annotations

import json
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
class WaitForMergeRetryTests(unittest.TestCase):
    """『Wait for merge and dispatch GitLab mirror』(id: wait-for-merge) の再試行。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_step_script("wait-for-merge")

    def _fake_bin(self, tmp: str, *, pr_view_sequence: list[dict], merge_exit: int = 0) -> tuple[str, str]:
        """gh / sleep を偽装する bin dir を作る。

        pr_view_sequence: `gh pr view` が呼ばれるたびに順番に返す JSON (dict) のリスト。
        呼び出された引数は /tmp/gh_calls.log にそのまま追記される。
        """
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        calls_log = os.path.join(tmp, "gh_calls.log")

        state_file = os.path.join(tmp, "pr_view_state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(pr_view_sequence, f)
        counter_file = os.path.join(tmp, "pr_view_counter")
        with open(counter_file, "w", encoding="utf-8") as f:
            f.write("0")

        gh_path = os.path.join(bindir, "gh")
        script = f"""#!/usr/bin/env python3
import json, os, sys

calls_log = {calls_log!r}
with open(calls_log, "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

if sys.argv[1:3] == ["pr", "view"]:
    state_file = {state_file!r}
    counter_file = {counter_file!r}
    with open(state_file, encoding="utf-8") as f:
        seq = json.load(f)
    with open(counter_file, encoding="utf-8") as f:
        i = int(f.read().strip())
    entry = seq[min(i, len(seq) - 1)]
    with open(counter_file, "w", encoding="utf-8") as f:
        f.write(str(i + 1))
    print(json.dumps(entry))
    sys.exit(0)

if sys.argv[1:3] == ["pr", "merge"]:
    sys.exit({merge_exit})

if sys.argv[1] == "workflow":
    sys.exit(0)

sys.stderr.write("unexpected gh invocation: " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(99)
"""
        with open(gh_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        os.chmod(gh_path, os.stat(gh_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        # 6 分ポーリングを実時間で待つとテストが遅いので sleep を no-op にする。
        sleep_path = os.path.join(bindir, "sleep")
        with open(sleep_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(sleep_path, os.stat(sleep_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        return bindir, calls_log

    def _run(self, *, pr_view_sequence: list[dict], merge_exit: int = 0) -> tuple[subprocess.CompletedProcess, str]:
        tmp = tempfile.mkdtemp()
        try:
            bindir, calls_log = self._fake_bin(tmp, pr_view_sequence=pr_view_sequence, merge_exit=merge_exit)
            script_path = os.path.join(tmp, "wait-for-merge.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.script)

            env = dict(
                os.environ,
                GH_TOKEN="dummy",
                PR_URL="https://github.com/omochairo/amazon/pull/7057",
                PR_NUMBER="7057",
                REPO="omochairo/amazon",
                PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
            )
            proc = subprocess.run([BASH, script_path], cwd=tmp, capture_output=True,
                                  text=True, env=env, timeout=30)
            calls = open(calls_log, encoding="utf-8").read() if os.path.exists(calls_log) else ""
            return proc, calls
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_retries_auto_merge_once_unstable_clears(self):
        """unstable (mergeStateStatus != CLEAN) の間は再試行せず待ち、CLEAN に
        なった時点で auto-merge 未登録なら `gh pr merge --auto` を再試行する。
        """
        proc, calls = self._run(pr_view_sequence=[
            {"state": "OPEN", "mergeStateStatus": "UNSTABLE", "autoMergeRequest": None,
             "headRefOid": "sha1"},
            {"state": "OPEN", "mergeStateStatus": "CLEAN", "autoMergeRequest": None,
             "headRefOid": "sha1"},
            {"state": "MERGED", "mergeStateStatus": "CLEAN", "autoMergeRequest": {"enabledAt": "x"},
             "headRefOid": "sha1"},
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("retrying enable (#7225)", proc.stdout)
        self.assertIn("--match-head-commit sha1", calls)
        self.assertIn("Dispatched.", proc.stdout)

    def test_does_not_retry_while_still_unstable(self):
        """mergeStateStatus が CLEAN に達するまでは `gh pr merge` を再試行しない
        (required checks 未完走のうちに叩いて GraphQL エラーを再生産しないため)。
        """
        proc, calls = self._run(pr_view_sequence=[
            {"state": "OPEN", "mergeStateStatus": "UNSTABLE", "autoMergeRequest": None,
             "headRefOid": "sha1"},
            {"state": "CLOSED", "mergeStateStatus": "UNSTABLE", "autoMergeRequest": None,
             "headRefOid": "sha1"},
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("pr merge", calls)
        self.assertIn("PR closed without merge", proc.stdout)

    def test_does_not_retry_when_already_armed(self):
        """auto-merge が既に登録済み (autoMergeRequest != null) なら再試行しない
        (二重に `gh pr merge --auto` を叩く必要は無い)。
        """
        proc, calls = self._run(pr_view_sequence=[
            {"state": "OPEN", "mergeStateStatus": "CLEAN", "autoMergeRequest": {"enabledAt": "x"},
             "headRefOid": "sha1"},
            {"state": "MERGED", "mergeStateStatus": "CLEAN", "autoMergeRequest": {"enabledAt": "x"},
             "headRefOid": "sha1"},
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("pr merge", calls)
        self.assertIn("Dispatched.", proc.stdout)


if __name__ == "__main__":
    unittest.main()
