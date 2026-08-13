"""05-jules-auto-merge.yml の scope ステップ (自動修復の可否判定) のテスト。

#5165: Jules が記事の出典調査に使った使い捨てスクリプトをリポジトリルートに
コミットすることがあり、そのたびに auto-merge のスコープガードに引っかかって
「チェックは緑なのにマージされない」状態で滞留していた (#5117)。気づかれずに
マージされたケースもある (#5035 の get_rakuten.py)。

対策として scope ステップに「スコープ外が新規追加ファイルだけなら機械的に
取り除く」判定 (repairable) を足した。ここはその判定の回帰テスト。

**ロジックをコピーせず、ワークフロー YAML から run スクリプトを実際に抜き出して
実行する。** コピーを検証しても本体がズレたら意味がないため。合成 git リポジトリ上で
BASE_SHA/HEAD_SHA を作り、GITHUB_OUTPUT に何が書かれるかを見る。
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
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "05-jules-auto-merge.yml"
ARTICLE = "data/articles/2026-08-14-B0CWH17Z6F.json"


def _bash() -> str | None:
    # CI (ubuntu) は /bin/bash、Windows 開発機は Git Bash を拾う。
    for candidate in ("/bin/bash", "bash"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


BASH = _bash()


def _load_scope_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["enable-auto-merge"]["steps"]:
        if step.get("id") == "scope":
            return step["run"]
    raise AssertionError("scope ステップが 05-jules-auto-merge.yml に見つからない")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class JulesAutoMergeScopeTests(unittest.TestCase):
    """scope ステップが eligible / repairable をどう決めるか。"""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_scope_script()

    def _run(self, mutate_cmd: str) -> dict[str, str]:
        """base コミットに mutate_cmd の変更を積み、scope スクリプトの出力を返す。"""
        tmp = tempfile.mkdtemp()
        try:
            setup = (
                "git init -q . && git config user.email t@example.com && git config user.name t "
                "&& mkdir -p data/articles scripts .github/workflows "
                "&& echo base > README.md && echo x > scripts/existing.py "
                "&& git add -A && git commit -qm base"
            )
            subprocess.run([BASH, "-lc", setup], cwd=tmp, capture_output=True, text=True, check=True)
            base = subprocess.run([BASH, "-lc", "git rev-parse HEAD"], cwd=tmp,
                                  capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run([BASH, "-lc", f"{mutate_cmd} && git add -A && git commit -qm change"],
                           cwd=tmp, capture_output=True, text=True, check=True)
            head = subprocess.run([BASH, "-lc", "git rev-parse HEAD"], cwd=tmp,
                                  capture_output=True, text=True, check=True).stdout.strip()

            out_path = os.path.join(tmp, "gh_output")
            open(out_path, "w", encoding="utf-8").close()
            script_path = os.path.join(tmp, "scope.sh")
            with open(script_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.script)

            env = dict(os.environ, BASE_SHA=base, HEAD_SHA=head,
                       GITHUB_OUTPUT=out_path.replace("\\", "/"))
            proc = subprocess.run([BASH, script_path], cwd=tmp, capture_output=True,
                                  text=True, env=env)
            self.assertEqual(proc.returncode, 0,
                             f"scope スクリプトが異常終了した\n{proc.stdout}\n{proc.stderr}")

            outputs: dict[str, str] = {}
            for line in open(out_path, encoding="utf-8"):
                if "=" in line and "<<" not in line:
                    key, _, value = line.strip().partition("=")
                    if key in ("eligible", "repairable"):
                        outputs[key] = value
            return outputs
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_article_only_is_eligible(self):
        """記事 JSON だけの通常の PR は従来どおり auto-merge 対象。"""
        out = self._run(f'echo "{{}}" > {ARTICLE}')
        self.assertEqual(out.get("eligible"), "true")

    def test_stray_root_script_is_repairable(self):
        """記事 JSON + ルート直下の使い捨てスクリプト → 機械的に取り除ける。"""
        out = self._run(f'echo "{{}}" > {ARTICLE} && echo scratch > get_sources.py')
        self.assertEqual(out.get("eligible"), "false")
        self.assertEqual(out.get("repairable"), "true")

    def test_modified_existing_file_is_not_repairable(self):
        """既存ファイルの変更が混ざる PR は自動修復しない (消すと作業が失われる)。"""
        out = self._run(f'echo "{{}}" > {ARTICLE} && echo changed >> scripts/existing.py')
        self.assertEqual(out.get("repairable"), "false")

    def test_deleted_existing_file_is_not_repairable(self):
        """既存ファイルの削除も同様に人間のレビューに回す。"""
        out = self._run(f'echo "{{}}" > {ARTICLE} && git rm -q scripts/existing.py')
        self.assertEqual(out.get("repairable"), "false")

    def test_github_dir_is_not_repairable(self):
        """.github/ 配下はワークフロー自体なので機械的に扱わない。"""
        out = self._run(f'echo "{{}}" > {ARTICLE} && echo "on: push" > .github/workflows/x.yml')
        self.assertEqual(out.get("repairable"), "false")

    def test_no_article_is_not_repairable(self):
        """記事 JSON が無い PR は記事レーンではないので触らない。"""
        out = self._run('echo scratch > get_sources.py')
        self.assertEqual(out.get("eligible"), "false")
        self.assertEqual(out.get("repairable"), "false")


class GitignoreBlocksRootScriptsTests(unittest.TestCase):
    """#5165: ルート直下の *.py が .gitignore で塞がれていること。

    元は /fix_*.py /tmp_*.py のような名前の接頭辞ブラックリストで、エージェントが
    新しい名前を使うたびに抜けていた (get_sources.py / get_rakuten.py)。
    """

    def test_root_py_is_ignored(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/*.py", gitignore.splitlines())

    def test_no_python_file_at_repo_root(self):
        """ルート直下に .py が復活していないこと (正規の Python は scripts/ 配下)。"""
        strays = sorted(p.name for p in REPO_ROOT.glob("*.py"))
        self.assertEqual(strays, [], f"ルート直下に .py がある: {strays}")


if __name__ == "__main__":
    unittest.main()
