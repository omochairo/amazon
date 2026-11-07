"""recover_pending_pr_checks.py の純粋関数を stdlib unittest で検証する (#6609)。

gh は呼ばない (evaluate_pr / filter_candidate_prs / required_checks_satisfied /
has_any_required_check は pure function)。
"""
import datetime as dt
import io
import os
import sys
import unittest
from unittest import mock
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # scripts/ を import path に追加

from recover_pending_pr_checks import (  # noqa: E402
    DEFAULT_PUSH_THRESHOLD_MINUTES,
    PR_FIELDS,
    STAGE2_LABEL,
    UNKNOWN_ESCALATE_MULTIPLIER,
    Action,
    _parse_commit_date_result,
    close_reopen_pr,
    deferred_unknown_escalated_message,
    dirty_message,
    emit_warning_annotation,
    evaluate_pr,
    filter_candidate_prs,
    has_any_required_check,
    has_stage2_label,
    recovered_none_message,
    required_checks_satisfied,
    reopen_env,
    resolve_push_time,
)

NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


def _rollup(**conclusions):
    """{"validate": "SUCCESS", "unit-tests": "FAILURE"} 形式から statusCheckRollup を作る。"""
    return [{"name": name, "conclusion": conclusion} for name, conclusion in conclusions.items()]


class RequiredChecksSatisfiedTests(unittest.TestCase):

    def test_both_success_is_satisfied(self):
        rollup = _rollup(validate="SUCCESS", **{"unit-tests": "SUCCESS"})
        self.assertTrue(required_checks_satisfied(rollup))

    def test_one_missing_is_not_satisfied(self):
        rollup = _rollup(validate="SUCCESS")
        self.assertFalse(required_checks_satisfied(rollup))

    def test_one_failing_is_not_satisfied(self):
        rollup = _rollup(validate="SUCCESS", **{"unit-tests": "FAILURE"})
        self.assertFalse(required_checks_satisfied(rollup))

    def test_empty_rollup_is_not_satisfied(self):
        self.assertFalse(required_checks_satisfied([]))

    def test_unrelated_checks_ignored(self):
        rollup = _rollup(**{"release-on-merge": "SUCCESS"})
        self.assertFalse(required_checks_satisfied(rollup))


class HasAnyRequiredCheckTests(unittest.TestCase):

    def test_present_regardless_of_conclusion(self):
        rollup = _rollup(validate="FAILURE")
        self.assertTrue(has_any_required_check(rollup))

    def test_absent(self):
        rollup = _rollup(**{"release-on-merge": "SUCCESS"})
        self.assertFalse(has_any_required_check(rollup))

    def test_empty(self):
        self.assertFalse(has_any_required_check([]))


class FilterCandidatePrsTests(unittest.TestCase):

    def _pr(self, number, **kw):
        pr = {"number": number, "isDraft": False, "isCrossRepository": False}
        pr.update(kw)
        return pr

    def test_excludes_draft(self):
        prs = [self._pr(1, isDraft=True), self._pr(2)]
        self.assertEqual([p["number"] for p in filter_candidate_prs(prs)], [2])

    def test_excludes_fork(self):
        prs = [self._pr(1, isCrossRepository=True), self._pr(2)]
        self.assertEqual([p["number"] for p in filter_candidate_prs(prs)], [2])

    def test_keeps_same_repo_non_draft(self):
        prs = [self._pr(1), self._pr(2)]
        self.assertEqual([p["number"] for p in filter_candidate_prs(prs)], [1, 2])


class PrFieldsRegressionTests(unittest.TestCase):
    """#6808: `commits` を積んだ `--limit 100` は GraphQL ノード上限で必ず失敗する。

    open PR が 0 件でも落ちる静的コスト解析なので、二度と `commits` を
    PR_FIELDS に足さないことを回帰ガードする。
    """

    def test_commits_field_is_not_requested(self):
        self.assertNotIn("commits", PR_FIELDS.split(","))


class ResolvePushTimeTests(unittest.TestCase):
    """push_time の取得 (`gh api` 呼び出し) を遅延させる条件を、fake で呼び出し回数を数えて検証する。"""

    def _fake_fetch(self):
        calls = []

        def fetch(head_sha):
            calls.append(head_sha)
            return NOW

        return fetch, calls

    def _resolve(self, fetch, **kw):
        base = dict(required_satisfied=False, has_any_check=False, stage2_done=False)
        base.update(kw)
        return resolve_push_time({"headRefOid": "sha123"}, fetch_commit_time=fetch, **base)

    def test_skips_fetch_when_required_satisfied(self):
        fetch, calls = self._fake_fetch()
        self._resolve(fetch, required_satisfied=True)
        self.assertEqual(calls, [])

    def test_skips_fetch_when_has_any_check(self):
        fetch, calls = self._fake_fetch()
        self._resolve(fetch, has_any_check=True)
        self.assertEqual(calls, [])

    def test_skips_fetch_when_stage2_done(self):
        fetch, calls = self._fake_fetch()
        self._resolve(fetch, stage2_done=True)
        self.assertEqual(calls, [])

    def test_fetches_exactly_once_when_all_clear(self):
        fetch, calls = self._fake_fetch()
        result = self._resolve(fetch)
        self.assertEqual(calls, ["sha123"])
        self.assertEqual(result, NOW)


class ParseCommitDateResultTests(unittest.TestCase):
    """commit 時刻取得が失敗 (非0 exit) しても例外を投げず None (= noop) になる。"""

    def test_success(self):
        result = _parse_commit_date_result(0, "2026-09-08T11:55:00Z\n", "", "sha123")
        self.assertEqual(result, dt.datetime(2026, 9, 8, 11, 55, tzinfo=dt.timezone.utc))

    def test_nonzero_exit_returns_none(self):
        result = _parse_commit_date_result(1, "", "not found", "sha123")
        self.assertIsNone(result)

    def test_empty_stdout_returns_none(self):
        result = _parse_commit_date_result(0, "", "", "sha123")
        self.assertIsNone(result)


class EmitWarningAnnotationTests(unittest.TestCase):
    """#7892: recovered_by=none を stdout の GitHub Actions annotation にも出す。"""

    def test_writes_workflow_command_to_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_warning_annotation("pr=123 recovered_by=none (close_reopen exhausted)")
        self.assertEqual(
            buf.getvalue(), "::warning::pr=123 recovered_by=none (close_reopen exhausted)\n"
        )


class RecoveredNoneMessageTests(unittest.TestCase):
    """#7919: close→reopen 済みでも直らない PR の文言が、dirty かどうかで分かれる。"""

    def test_conflicting_mentions_dirty_not_generic_needs_human(self):
        message = recovered_none_message(123, "CONFLICTING")
        self.assertIn("pr=123", message)
        self.assertIn("dirty", message)
        self.assertNotIn("needs human", message)

    def test_non_conflicting_keeps_generic_needs_human(self):
        message = recovered_none_message(123, "MERGEABLE")
        self.assertIn("needs human", message)
        self.assertNotIn("dirty", message)

    def test_missing_mergeable_keeps_generic_needs_human(self):
        message = recovered_none_message(123, None)
        self.assertIn("needs human", message)


class DirtyMessageTests(unittest.TestCase):

    def test_mentions_pr_number_and_conflicting(self):
        message = dirty_message(123)
        self.assertIn("pr=123", message)
        self.assertIn("CONFLICTING", message)


class DeferredUnknownEscalatedMessageTests(unittest.TestCase):

    def test_mentions_pr_number_and_unknown(self):
        message = deferred_unknown_escalated_message(123)
        self.assertIn("pr=123", message)
        self.assertIn("UNKNOWN", message)

    def test_reports_minutes_since_push_without_claiming_stuck(self):
        message = deferred_unknown_escalated_message(123, 187)
        self.assertIn("187 min", message)
        self.assertNotIn("stuck", message)


class HasStage2LabelTests(unittest.TestCase):

    def test_present(self):
        pr = {"labels": [{"name": STAGE2_LABEL}]}
        self.assertTrue(has_stage2_label(pr))

    def test_absent(self):
        pr = {"labels": [{"name": "auto-merge-skipped"}]}
        self.assertFalse(has_stage2_label(pr))

    def test_no_labels_key(self):
        self.assertFalse(has_stage2_label({}))


class EvaluatePrTests(unittest.TestCase):
    """close→reopen 一本化後の状態遷移をすべて網羅する (#6609 2026-09-10 の設計変更)。"""

    def _eval(self, **kw):
        base = dict(
            required_satisfied=False,
            has_any_check=False,
            stage2_done=False,
            push_time=NOW - dt.timedelta(minutes=10),
            now=NOW,
        )
        base.update(kw)
        return evaluate_pr(**base)

    def test_below_push_threshold_is_noop(self):
        """閾値未満は対象外。"""
        action = self._eval(push_time=NOW - dt.timedelta(minutes=2))
        self.assertEqual(action, Action("noop"))

    def test_push_threshold_boundary_closes_and_reopens(self):
        action = self._eval(push_time=NOW - dt.timedelta(minutes=5))
        self.assertEqual(action, Action("close_reopen"))

    def test_already_has_required_check_is_noop(self):
        """既に checks がある PR は対象外 (pull_request が発火済み = 配送漏れではない)。"""
        action = self._eval(has_any_check=True)
        self.assertEqual(action, Action("noop"))

    def test_unknown_push_time_is_noop(self):
        action = self._eval(push_time=None)
        self.assertEqual(action, Action("noop"))

    def test_eligible_after_threshold_closes_and_reopens(self):
        action = self._eval(push_time=NOW - dt.timedelta(minutes=6))
        self.assertEqual(action, Action("close_reopen"))

    def test_stage2_done_and_now_satisfied_is_recovered_close_reopen(self):
        action = self._eval(stage2_done=True, required_satisfied=True)
        self.assertEqual(action, Action("recovered_close_reopen"))

    def test_stage2_done_and_still_unsatisfied_gives_up(self):
        """close→reopen を1回しかやらない: 済みでまだ満たされなければ recovered_by=none で停止。"""
        action = self._eval(stage2_done=True)
        self.assertEqual(action, Action("recovered_none"))

    def test_stage2_done_never_returns_close_reopen_again(self):
        action = self._eval(stage2_done=True)
        self.assertNotEqual(action.kind, "close_reopen")

    def test_both_recovered_by_values_are_reachable(self):
        kinds = {
            self._eval(stage2_done=True, required_satisfied=True).kind,
            self._eval(stage2_done=True, required_satisfied=False).kind,
        }
        self.assertEqual(kinds, {"recovered_close_reopen", "recovered_none"})

    def test_satisfied_without_any_intervention_history_is_noop(self):
        """通常配送 (pull_request が普通に発火) で満たされた場合は復旧対象ではない。"""
        action = self._eval(required_satisfied=True)
        self.assertEqual(action, Action("noop"))

    def test_conflicting_mergeable_is_dirty_not_close_reopen(self):
        """#7912: base が古く dirty な PR は close→reopen を消費せず dirty を返す。"""
        action = self._eval(mergeable="CONFLICTING")
        self.assertEqual(action, Action("dirty"))

    def test_unknown_mergeable_defers_without_escalation(self):
        """#7912/#7919: mergeable=UNKNOWN は dirty と誤判定せず deferred_unknown に持ち越す。

        経過時間が UNKNOWN_ESCALATE_MULTIPLIER 倍の閾値に達していなければ
        escalate しない (push threshold ちょうど = 5分後、escalate 閾値は 30分後)。
        """
        action = self._eval(mergeable="UNKNOWN", push_time=NOW - dt.timedelta(minutes=6))
        self.assertEqual(action, Action("deferred_unknown", escalate=False))

    def test_unknown_mergeable_past_escalate_threshold_escalates(self):
        """#7919: push から push_threshold_minutes × UNKNOWN_ESCALATE_MULTIPLIER 以上
        経ってもなお UNKNOWN なら escalate=True にする。"""
        minutes = DEFAULT_PUSH_THRESHOLD_MINUTES * UNKNOWN_ESCALATE_MULTIPLIER
        action = self._eval(mergeable="UNKNOWN", push_time=NOW - dt.timedelta(minutes=minutes))
        self.assertEqual(action, Action("deferred_unknown", escalate=True))

    def test_unknown_mergeable_just_below_escalate_threshold_does_not_escalate(self):
        minutes = DEFAULT_PUSH_THRESHOLD_MINUTES * UNKNOWN_ESCALATE_MULTIPLIER - 1
        action = self._eval(mergeable="UNKNOWN", push_time=NOW - dt.timedelta(minutes=minutes))
        self.assertEqual(action, Action("deferred_unknown", escalate=False))

    def test_mergeable_mergeable_still_closes_and_reopens(self):
        """#7912: mergeable=MERGEABLE (衝突無し) は従来通り close→reopen。"""
        action = self._eval(mergeable="MERGEABLE")
        self.assertEqual(action, Action("close_reopen"))

    def test_mergeable_unset_still_closes_and_reopens(self):
        """後方互換: mergeable を渡さない呼び出しは従来通り close→reopen のまま。"""
        action = self._eval()
        self.assertEqual(action, Action("close_reopen"))

    def test_conflicting_below_push_threshold_is_still_noop(self):
        """dirty 判定より push threshold の方が先に効く (誤爆防止が優先)。"""
        action = self._eval(mergeable="CONFLICTING", push_time=NOW - dt.timedelta(minutes=2))
        self.assertEqual(action, Action("noop"))

    def test_conflicting_with_stage2_done_is_recovered_none(self):
        """既に close→reopen 済み (stage2_done) なら dirty 判定より優先して従来通り。"""
        action = self._eval(mergeable="CONFLICTING", stage2_done=True)
        self.assertEqual(action, Action("recovered_none"))


class PrFieldsMergeableRegressionTests(unittest.TestCase):
    """#7912: mergeable を PR_FIELDS に積んでいないと dirty PR を判定できない。"""

    def test_mergeable_field_is_requested(self):
        self.assertIn("mergeable", PR_FIELDS.split(","))


class ReopenTokenTest(unittest.TestCase):
    """#8092: close→reopen は App token (REOPEN_GH_TOKEN) で行う。

    GITHUB_TOKEN の reopen は pull_request を発火しないため、回復実績が 0 件だった。
    """

    def test_reopen_env_swaps_gh_token(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ambient", "REOPEN_GH_TOKEN": "app"}):
            env = reopen_env()
        self.assertEqual(env["GH_TOKEN"], "app")

    def test_reopen_env_without_app_token_warns_and_keeps_ambient(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ambient"}, clear=True), redirect_stdout(buf):
            env = reopen_env()
        self.assertIsNone(env)
        self.assertIn("REOPEN_GH_TOKEN unset", buf.getvalue())

    def test_close_and_reopen_use_app_token_but_label_edit_does_not_need_it(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ambient", "REOPEN_GH_TOKEN": "app"}),                 mock.patch("recover_pending_pr_checks.subprocess.run") as run:
            close_reopen_pr("o/r", 123)
        calls = {c.args[0][2]: c.kwargs.get("env") for c in run.call_args_list if c.args[0][1] == "pr"}
        self.assertEqual(calls["close"]["GH_TOKEN"], "app")
        self.assertEqual(calls["reopen"]["GH_TOKEN"], "app")




class EvaluateAutoMergeTests(unittest.TestCase):
    """required check 済みで auto-merge が外れた記事 PR の復旧判定 (2026-11-07)。

    実例: #11125 / #11289 (05 の有効化が GitHub の 500 で失敗 → 再実行で直った)、
    #11124 (PR の head が古いコミットで止まり、close→reopen で直った)。
    """

    HEAD = "a" * 40

    def _eval(self, **kw):
        from recover_pending_pr_checks import evaluate_auto_merge
        args = dict(
            required_satisfied=True,
            auto_merge_armed=False,
            labels=[],
            mergeable="MERGEABLE",
            pr_head_sha=self.HEAD,
            branch_sha=self.HEAD,
            last_run={"status": "completed", "conclusion": "success",
                      "updatedAt": "2026-09-08T11:00:00Z", "headSha": self.HEAD,
                      "databaseId": 1},
            now=NOW,
        )
        args.update(kw)
        return evaluate_auto_merge(**args)

    def test_checks_passed_but_not_armed_is_rerun(self):
        self.assertEqual(self._eval(), "rerun")

    def test_pr_head_behind_branch_is_desync(self):
        self.assertEqual(self._eval(branch_sha="b" * 40), "desync")

    def test_already_armed_or_checks_not_passed_is_noop(self):
        self.assertEqual(self._eval(auto_merge_armed=True), "noop")
        self.assertEqual(self._eval(required_satisfied=False), "noop")

    def test_not_an_auto_merge_target_is_noop(self):
        # 05 が一度も走っていない PR (記事以外) と、05 がスコープ外と判定した PR
        self.assertEqual(self._eval(last_run=None), "noop")
        self.assertEqual(self._eval(labels=["auto-merge-skipped"]), "noop")

    def test_conflicting_is_noop(self):
        self.assertEqual(self._eval(mergeable="CONFLICTING"), "noop")

    def test_waits_while_05_is_running_or_just_finished(self):
        running = {"status": "in_progress", "updatedAt": "2026-09-08T11:00:00Z",
                   "headSha": self.HEAD, "databaseId": 1}
        self.assertEqual(self._eval(last_run=running), "noop")
        just = {"status": "completed", "updatedAt": "2026-09-08T11:50:00Z",
                "headSha": self.HEAD, "databaseId": 1}
        self.assertEqual(self._eval(last_run=just), "noop")

    def test_newer_head_without_its_05_run_waits(self):
        old = {"status": "completed", "updatedAt": "2026-09-08T11:00:00Z",
               "headSha": "c" * 40, "databaseId": 1}
        self.assertEqual(self._eval(last_run=old), "noop")

    def test_only_once_per_pr(self):
        self.assertEqual(self._eval(labels=["auto-merge-recovered"]), "exhausted")
        self.assertEqual(
            self._eval(labels=["auto-merge-recovered"], branch_sha="b" * 40), "exhausted")

    def test_pr_fields_include_auto_merge_request(self):
        self.assertIn("autoMergeRequest", PR_FIELDS)


class RecoverAutoMergeIoTests(unittest.TestCase):
    """I/O 層: 再実行・close→reopen を呼ぶ順と、dry-run で何もしないこと。"""

    def _pr(self, **kw):
        pr = {"number": 7, "headRefName": "feat/x", "headRefOid": "a" * 40,
              "statusCheckRollup": [{"name": "validate", "conclusion": "SUCCESS"},
                                    {"name": "unit-tests", "conclusion": "SUCCESS"}],
              "labels": [], "mergeable": "MERGEABLE", "autoMergeRequest": None}
        pr.update(kw)
        return pr

    def _run(self, prs, branch_sha, dry_run=False):
        import recover_pending_pr_checks as m
        last = {"status": "completed", "updatedAt": "2026-09-08T11:00:00Z",
                "headSha": "a" * 40, "databaseId": 99}
        with mock.patch.object(m, "fetch_last_auto_merge_run", return_value=last), \
                mock.patch.object(m, "fetch_branch_sha", return_value=branch_sha), \
                mock.patch.object(m, "mark_auto_merge_recovered") as mark, \
                mock.patch.object(m, "rerun_auto_merge") as rerun, \
                mock.patch.object(m.subprocess, "run") as run, \
                mock.patch.object(m, "reopen_env", return_value=None):
            counts = m.recover_auto_merge("o/r", prs, NOW, dry_run)
        return counts, mark, rerun, run

    def test_rerun_marks_then_reruns_05(self):
        counts, mark, rerun, run = self._run([self._pr()], "a" * 40)
        self.assertEqual(counts["rerun"], 1)
        mark.assert_called_once_with("o/r", 7)
        rerun.assert_called_once_with("o/r", 99)
        run.assert_not_called()

    def test_desync_closes_and_reopens(self):
        counts, mark, rerun, run = self._run([self._pr()], "b" * 40)
        self.assertEqual(counts["desync"], 1)
        rerun.assert_not_called()
        cmds = [c.args[0][:3] for c in run.call_args_list]
        self.assertEqual(cmds, [["gh", "pr", "close"], ["gh", "pr", "reopen"]])

    def test_dry_run_changes_nothing(self):
        counts, mark, rerun, run = self._run([self._pr()], "a" * 40, dry_run=True)
        self.assertEqual(counts["rerun"], 1)
        mark.assert_not_called()
        rerun.assert_not_called()
        run.assert_not_called()

    def test_armed_pr_is_not_even_queried(self):
        import recover_pending_pr_checks as m
        with mock.patch.object(m, "fetch_last_auto_merge_run") as last:
            m.recover_auto_merge("o/r", [self._pr(autoMergeRequest={"x": 1})], NOW, False)
        last.assert_not_called()


if __name__ == "__main__":
    unittest.main()
