"""04-validate-article-pr.yml の差分範囲が 3 点 (base...head) であることの回帰テスト。

#6793 で入れた「data/articles/ 以外の削除を検出する」ゲートは、導入時に 2 点差分
(`git diff base head`) を使っていた。`pull_request.base.sha` は **イベント時点の
main の先端** なので、PR が分岐した後に main へ入ったファイルは 2 点差分では
すべて「この PR が削除した」ように見える。

2026-09-08 の実測 (#6840): 3 点の削除 0 件に対し 2 点は 10 件以上を検出し、うち
`data/raw/per_asin/B0H8BPQHQN/experience.json` は PR #6836 が main に足したもの
だった。結果として **main が動くだけで無関係な記事 PR が落ちる** (#6840 / #6832)。

ロジックをコピーせず、ワークフロー YAML から run スクリプトを実際に抜き出して
合成 git リポジトリ上で実行する (test_jules_auto_merge_scope.py と同じ方針)。
コピーを検証しても本体がズレたら意味がないため。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "04-validate-article-pr.yml"
CHECKER = REPO_ROOT / "scripts" / "check_article_pr_deletions.py"


def _bash() -> str | None:
    for candidate in ("/bin/bash", "bash"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


BASH = _bash()


def _step_run(name_contains: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if name_contains in (step.get("name") or ""):
                return step["run"]
    raise AssertionError(f"{name_contains} を含む step が {WORKFLOW.name} に見つからない")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class DeletionCheckRangeTests(unittest.TestCase):
    """削除検出ステップが「PR 自身の削除」だけを見ているか。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _step_run("Check for deletions outside data/articles/")

    def _run(self, *, main_cmd: str, pr_cmd: str):
        """base から分岐した PR ブランチを作り、main 側にも main_cmd の変更を積む。

        戻り値は削除検出ステップの CompletedProcess。
        """
        tmp = tempfile.mkdtemp()
        try:
            def sh(cmd, check=True):
                return subprocess.run([BASH, "-lc", cmd], cwd=tmp, capture_output=True,
                                      text=True, check=check)

            sh("git init -q -b main . && git config user.email t@example.com "
               "&& git config user.name t "
               "&& mkdir -p data/articles data/raw/per_asin/B0OLD scripts "
               "&& echo '{}' > data/articles/2026-01-01-B0OLD00000.json "
               "&& echo '{}' > data/raw/per_asin/B0OLD/omcha_related.json "
               "&& git add -A && git commit -qm base")
            # PR ブランチは base から分岐する
            sh(f"git checkout -q -b pr && {pr_cmd} && git add -A && git commit -qm pr")
            head = sh("git rev-parse HEAD").stdout.strip()
            # main はその後さらに進む (base.sha はこちらを指す)
            sh(f"git checkout -q main && {main_cmd} && git add -A && git commit -qm main")
            base = sh("git rev-parse HEAD").stdout.strip()

            os.makedirs(os.path.join(tmp, "scripts"), exist_ok=True)
            shutil.copy(CHECKER, os.path.join(tmp, "scripts", CHECKER.name))

            script_path = os.path.join(tmp, "step.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.script)
            env = dict(os.environ, BASE=base, HEAD=head)
            return subprocess.run([BASH, script_path], cwd=tmp, capture_output=True,
                                  text=True, env=env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_file_added_to_main_after_branching_is_not_a_deletion(self):
        """#6840 の再現: main 側の新規ファイルを「PR による削除」と誤検出しない。"""
        proc = self._run(
            main_cmd="mkdir -p data/raw/per_asin/B0NEW "
                     "&& echo '{}' > data/raw/per_asin/B0NEW/experience.json",
            pr_cmd="echo '{}' > data/articles/2026-09-09-B0NEWPR0001.json",
        )
        self.assertEqual(proc.returncode, 0,
                         f"main 側の追加を削除と誤検出した\n{proc.stdout}\n{proc.stderr}")

    def test_real_deletion_outside_articles_still_fails(self):
        """本来の目的 (#6788 の巻き添え削除) は引き続き検出する。"""
        proc = self._run(
            main_cmd="echo x >> README.md || echo x > README.md",
            pr_cmd="echo '{}' > data/articles/2026-09-09-B0NEWPR0001.json "
                   "&& git rm -q data/raw/per_asin/B0OLD/omcha_related.json",
        )
        self.assertEqual(proc.returncode, 1, f"巻き添え削除を見逃した\n{proc.stdout}")
        self.assertIn("omcha_related.json", proc.stderr)

    def test_deletion_inside_articles_is_allowed(self):
        """data/articles/ 配下の削除 (rewrite の新旧入れ替え等) は許可。"""
        proc = self._run(
            main_cmd="echo x > README.md",
            pr_cmd="echo '{}' > data/articles/2026-09-09-B0NEWPR0001.json "
                   "&& git rm -q data/articles/2026-01-01-B0OLD00000.json",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class RangeSyntaxTests(unittest.TestCase):
    """範囲指定が 3 点であることを YAML 側でも固定する (誤って戻されないように)。"""

    def test_both_diff_steps_use_three_dot_range(self):
        for name in ("List changed article JSON",
                     "Check for deletions outside data/articles/"):
            run = _step_run(name)
            self.assertIn('"$BASE...$HEAD"', run,
                          f"{name} が 3 点範囲を使っていない")
            self.assertNotIn('"$BASE" "$HEAD"', run,
                             f"{name} が 2 点範囲に戻っている")
