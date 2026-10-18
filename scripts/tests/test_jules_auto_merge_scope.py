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

    @staticmethod
    def _parse_github_output(out_path: str) -> dict[str, str]:
        """`$GITHUB_OUTPUT` を単純な key=value と key<<EOF ヒアドキュメントの両方に対応して読む。"""
        outputs: dict[str, str] = {}
        lines = open(out_path, encoding="utf-8").read().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:
                key, _, delim = line.partition("<<")
                i += 1
                body: list[str] = []
                while i < len(lines) and lines[i] != delim:
                    body.append(lines[i])
                    i += 1
                outputs[key] = "\n".join(body)
            elif "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
            i += 1
        return outputs

    def _run(self, mutate_cmd: str) -> dict[str, str]:
        """base コミットに mutate_cmd の変更を積み、scope スクリプトの出力を返す。"""
        tmp = tempfile.mkdtemp()
        try:
            setup = (
                "git init -q . && git config user.email t@example.com && git config user.name t "
                "&& mkdir -p data/articles data/raw scripts .github/workflows "
                "&& echo base > README.md && echo x > scripts/existing.py "
                "&& echo '{\"items\": []}' > data/raw/rakuten_matched.json "
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

            return self._parse_github_output(out_path)
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

    def test_modified_data_raw_file_is_repairable_via_restore(self):
        """#8033: Jules が読み取り専用の data/raw/ 既存ファイルを書き換えても、
        base へ戻せば安全に外せるので repairable=true とし、restore に載せる。"""
        out = self._run(
            f'echo "{{}}" > {ARTICLE} && '
            'echo \'{"items": [{"x": 1}]}\' > data/raw/rakuten_matched.json'
        )
        self.assertEqual(out.get("repairable"), "true")
        self.assertEqual(out.get("restore"), "data/raw/rakuten_matched.json")

    def test_deleted_data_raw_file_is_repairable_via_restore(self):
        """data/raw/ 既存ファイルの削除も同様に base へ戻せば安全。"""
        out = self._run(f'echo "{{}}" > {ARTICLE} && git rm -q data/raw/rakuten_matched.json')
        self.assertEqual(out.get("repairable"), "true")
        self.assertEqual(out.get("restore"), "data/raw/rakuten_matched.json")

    def test_data_raw_change_mixed_with_non_restorable_stays_blocked(self):
        """data/raw/ の変更と、それ以外の既存ファイル変更が混ざるなら人間に回す。"""
        out = self._run(
            f'echo "{{}}" > {ARTICLE} && '
            'echo \'{"items": [{"x": 1}]}\' > data/raw/rakuten_matched.json && '
            'echo changed >> scripts/existing.py'
        )
        self.assertEqual(out.get("repairable"), "false")


@unittest.skipIf(BASH is None, "bash が無い環境ではスキップ")
class MainCopiesTests(unittest.TestCase):
    """#9638: PR ブランチが main の新しい内容を普通のコミットとして取り込んだ場合。

    PR #9685 では、分岐後に main へ入った他レーンの記事・experience.json・
    asin_origin.jsonl などがそのまま Jules のコミットに入り、triple-dot で
    「この PR の変更」に見えてスコープ外 → auto-merge-skipped で滞留した。
    """

    OTHER_ARTICLE = "data/articles/2026-08-14-B0AAAAAAAA.json"

    @classmethod
    def setUpClass(cls):
        cls.script = _load_scope_script()

    def _run(self, main_cmds: list[str], branch_cmd: str) -> dict[str, str]:
        """base から PR ブランチを切り、main 側に main_cmds を 1 コミットずつ積んだうえで
        PR ブランチに branch_cmd を積む (branch_cmd からは `main` / `main~N` を参照できる)。"""
        tmp = tempfile.mkdtemp()

        def sh(cmd: str) -> str:
            return subprocess.run([BASH, "-lc", cmd], cwd=tmp, capture_output=True,
                                  text=True, check=True).stdout.strip()

        try:
            sh("git init -q -b main . && git config user.email t@example.com && git config user.name t "
               "&& mkdir -p data/articles data/raw data/analytics scripts "
               "&& echo base > README.md && echo a > data/analytics/asin_origin.jsonl "
               "&& echo 'k: v' > data/term_slugs.yaml "
               "&& git add -A && git commit -qm base && git branch pr")
            for i, cmd in enumerate(main_cmds):
                sh(f"{cmd} && git add -A && git commit -qm main{i}")
            base = sh("git rev-parse main")
            # pr へ切り替えると main 側で増えた記事ごと data/articles/ が消えることがある
            sh(f"git checkout -q pr && mkdir -p data/articles && {branch_cmd} "
               "&& git add -A && git commit -qm change")
            head = sh("git rev-parse HEAD")

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
            return JulesAutoMergeScopeTests._parse_github_output(out_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    MAIN_LANES = [
        f'echo "{{}}" > {OTHER_ARTICLE}',
        "echo b >> data/analytics/asin_origin.jsonl && echo 'k2: v2' >> data/term_slugs.yaml",
    ]

    def test_copies_of_latest_main_are_not_this_prs_changes(self):
        """main の最新をそのまま写したファイルは除外され、記事 PR として扱われる。"""
        out = self._run(
            self.MAIN_LANES,
            f'echo "{{}}" > {ARTICLE} && git checkout main -- {self.OTHER_ARTICLE} '
            'data/analytics/asin_origin.jsonl data/term_slugs.yaml',
        )
        self.assertEqual(out.get("eligible"), "true")
        self.assertEqual(out.get("repairable"), "true")  # 写しを戻すための修復
        self.assertEqual(out.get("out_of_scope", ""), "")
        self.assertEqual(
            out.get("main_copies", "").splitlines(),
            sorted(["data/analytics/asin_origin.jsonl", "data/term_slugs.yaml", self.OTHER_ARTICLE]),
        )

    def test_copy_of_intermediate_main_version_is_also_detected(self):
        """main の途中の版 (取り込んだ後に main がさらに進んだ) も写しとして扱う。"""
        out = self._run(
            self.MAIN_LANES + ["echo 'k3: v3' >> data/term_slugs.yaml"],
            f'echo "{{}}" > {ARTICLE} && git show main~1:data/term_slugs.yaml > data/term_slugs.yaml',
        )
        self.assertEqual(out.get("eligible"), "true")
        self.assertEqual(out.get("main_copies"), "data/term_slugs.yaml")

    def test_own_edit_of_main_file_stays_out_of_scope(self):
        """main のどの版とも一致しない変更はこの PR の作業なので、従来どおり人間に回す。"""
        out = self._run(
            self.MAIN_LANES,
            f'echo "{{}}" > {ARTICLE} && echo mine >> data/term_slugs.yaml',
        )
        self.assertEqual(out.get("eligible"), "false")
        self.assertEqual(out.get("repairable"), "false")
        self.assertEqual(out.get("out_of_scope"), "data/term_slugs.yaml")
        self.assertEqual(out.get("main_copies", ""), "")

    def test_copied_article_is_not_counted_as_this_prs_article(self):
        """他レーンの記事の写ししか無い PR は記事 PR ではない。"""
        out = self._run(self.MAIN_LANES, f"git checkout main -- {self.OTHER_ARTICLE}")
        self.assertEqual(out.get("eligible"), "false")
        self.assertNotEqual(out.get("repairable"), "true")

    def test_plain_article_pr_has_no_main_copies(self):
        """main が進んでいても、写しの無い通常の記事 PR は従来どおり。"""
        out = self._run(self.MAIN_LANES, f'echo "{{}}" > {ARTICLE}')
        self.assertEqual(out.get("eligible"), "true")
        self.assertEqual(out.get("main_copies", ""), "")
        self.assertNotIn("repairable", out)


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
