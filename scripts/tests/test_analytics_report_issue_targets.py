"""17-analytics-report.yml の Issue 起票先のテスト (#4793 / #5284)。

このレーンは 5 週以上にわたり、GA4/GSC の実数値を含む Issue を **public リポジトリ**へ
毎週自動起票していた。#5284 で実数値を出す経路を private (amazon-navi-brain) へ移したが、
**配線が正しいことを実走まで確かめられない**のが元の事故と同じ構造なので、ここで固定する。

このテストが守るのは「どこへ起票するか」の配線であって、実行時の資格情報ではない。
`NAVI_BRAIN_PAT` に issues:write が付いているかは secret を読めないため原理的に
テストできない (実走の preflight とその後の起票ステップだけが答えを持つ)。
テストできる範囲とできない範囲を混同しないこと。

設計上いちばん効かせたいのは PUBLIC_SAFE_SCRIPTS の allowlist。**新しい検出器を足した
ときは既定で失敗する**ようにしてあり、追加した人に「その Issue は実数値を含むか」を
必ず一度は判断させる。含まないと判断したときだけ allowlist に足す。
"""
from __future__ import annotations

import pathlib
import re
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "17-analytics-report.yml"

PUBLIC_REPO = "omochairo/amazon"
PRIVATE_REPO = "omochairo/amazon-navi-brain"

# 実数値を出さないので public のままでよい起票スクリプト。
#
# A-6 (brand_taxonomy 追加候補) と P3 (カタログ未登録ブランド候補) は候補の件数と
# ASIN しか出さず、GA4/GSC 由来の指標を持たない。かつ CLAUDE.md「TODO は Issues に
# 置く」で公開側の一次管理対象になっている作業項目なので、private に移すと TODO 一覧
# から消えて追跡できなくなる。
PUBLIC_SAFE_SCRIPTS = frozenset({
    "open_brand_suggestion_issue.py",
    "open_catalog_brand_issue.py",
})

# private 側 (amazon-navi-brain) に実際に作成済みのラベル。
# gh issue create --label は未定義ラベルで落ちるため、起票スクリプトが使うラベルが
# この集合に収まっていることを固定する。ラベルを増やすときは private 側にも作ること。
PROVISIONED_LABELS = frozenset({"quality", "todo", "observation", "analytics"})

_SCRIPT_RE = re.compile(r"python\s+(scripts/(open_\w+\.py))")


def _load_steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["report"]["steps"]


def _job_env() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["report"].get("env") or {}


def _issue_steps() -> list[tuple[str, str, dict]]:
    """(script 名, step 名, step の env) を返す。open_*.py を走らせる step のみ。"""
    out = []
    for step in _load_steps():
        run = step.get("run") or ""
        m = _SCRIPT_RE.search(run)
        if m:
            out.append((m.group(2), step.get("name", ""), step.get("env") or {}))
    return out


class DestinationTest(unittest.TestCase):
    def test_workflow_exists(self):
        self.assertTrue(WORKFLOW.exists(), f"{WORKFLOW} が無い")

    def test_destination_is_defined_once_at_job_level(self):
        # 宛先が複数箇所に散ると、片方だけ直して public に残る事故が起きる。
        self.assertEqual(_job_env().get("ANALYTICS_ISSUE_REPO"), PRIVATE_REPO)

    def test_destination_is_not_the_public_repo(self):
        self.assertNotEqual(_job_env().get("ANALYTICS_ISSUE_REPO"), PUBLIC_REPO)

    def test_metric_bearing_scripts_go_to_the_private_repo(self):
        found = _issue_steps()
        self.assertTrue(found, "open_*.py を走らせる step が 1 つも見つからない")
        for script, name, env in found:
            if script in PUBLIC_SAFE_SCRIPTS:
                continue
            with self.subTest(script=script, step=name):
                self.assertEqual(
                    env.get("REPO"), "${{ env.ANALYTICS_ISSUE_REPO }}",
                    f"{script} は実数値を含む想定なので private 宛でなければならない。"
                    f"実数値を含まないなら PUBLIC_SAFE_SCRIPTS に足すこと",
                )
                self.assertEqual(
                    env.get("GH_TOKEN"), "${{ secrets.NAVI_BRAIN_PAT }}",
                    f"{script}: cross-repo 起票に GITHUB_TOKEN は使えない",
                )

    def test_public_safe_scripts_stay_public(self):
        # 逆方向の退行 (公開 TODO まで private に飲み込む) も止める。
        for script, name, env in _issue_steps():
            if script not in PUBLIC_SAFE_SCRIPTS:
                continue
            with self.subTest(script=script, step=name):
                self.assertEqual(env.get("REPO"), "${{ github.repository }}")
                self.assertEqual(env.get("GH_TOKEN"), "${{ secrets.GITHUB_TOKEN }}")

    def test_weekly_report_step_targets_the_private_repo(self):
        steps = [s for s in _load_steps() if "gh issue create" in (s.get("run") or "")]
        self.assertEqual(len(steps), 1, "週次レポートの起票 step は 1 つの想定")
        step = steps[0]
        run = step["run"]
        self.assertIn("$ANALYTICS_ISSUE_REPO", run,
                      "週次レポートは PV/CTR/掲載順位の実数そのものなので private 宛")
        self.assertNotIn("github.repository", run)
        self.assertEqual((step.get("env") or {}).get("GH_TOKEN"),
                         "${{ secrets.NAVI_BRAIN_PAT }}")


class PreflightTest(unittest.TestCase):
    def _preflight(self) -> dict:
        for step in _load_steps():
            if str(step.get("name", "")).startswith("Preflight"):
                return step
        self.fail("Preflight step が無い")

    def test_preflight_exists_and_uses_the_pat(self):
        step = self._preflight()
        self.assertEqual((step.get("env") or {}).get("GH_TOKEN"),
                         "${{ secrets.NAVI_BRAIN_PAT }}")

    def test_preflight_fails_loudly_instead_of_skipping(self):
        # #4793 の核心。skip して緑終了させると、PAT 失効の週から「実数値の Issue が
        # 1 件も立たない」状態が無音で続く。
        step = self._preflight()
        run = step["run"]
        self.assertIn("exit 1", run, "資格情報が無いとき落ちなければ無音で死ぬ")
        self.assertIn("::error::", run)
        self.assertNotIn("::notice::", run, "notice で skip させない")
        self.assertNotIn("continue-on-error", str(step),
                         "preflight を握りつぶすと存在意義が無くなる")

    def test_preflight_runs_before_any_issue_creation(self):
        names = [str(s.get("name", "")) for s in _load_steps()]
        runs = [str(s.get("run") or "") for s in _load_steps()]
        pre = next(i for i, n in enumerate(names) if n.startswith("Preflight"))
        first_issue = min(
            i for i, r in enumerate(runs)
            if _SCRIPT_RE.search(r) or "gh issue create" in r
        )
        self.assertLess(pre, first_issue, "preflight は起票より前に置く")

    def test_preflight_runs_after_the_data_commit_back(self):
        # ここで落ちても収集済みデータが失われないことを配置で保証する。
        runs = [str(s.get("run") or "") for s in _load_steps()]
        names = [str(s.get("name", "")) for s in _load_steps()]
        pre = next(i for i, n in enumerate(names) if n.startswith("Preflight"))
        pushes = [i for i, r in enumerate(runs) if "git push" in r]
        self.assertTrue(pushes, "commit-back step が見つからない")
        self.assertGreater(pre, min(pushes),
                           "preflight がデータ commit-back より前だと、"
                           "起票できない週は収集済みデータごと落ちる")


class LabelTest(unittest.TestCase):
    def test_routed_scripts_only_use_labels_provisioned_on_the_private_repo(self):
        for script, _name, env in _issue_steps():
            if script in PUBLIC_SAFE_SCRIPTS:
                continue
            path = REPO_ROOT / "scripts" / script
            with self.subTest(script=script):
                self.assertTrue(path.exists())
                m = re.search(r'^LABELS\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"),
                              re.MULTILINE)
                self.assertIsNotNone(m, f"{script}: LABELS 定数が読めない")
                labels = {x.strip() for x in m.group(1).split(",") if x.strip()}
                missing = labels - PROVISIONED_LABELS
                self.assertFalse(
                    missing,
                    f"{script}: {sorted(missing)} は private 側に未作成。"
                    f"gh issue create --label は未定義ラベルで落ちる",
                )


if __name__ == "__main__":
    unittest.main()
