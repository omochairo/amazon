"""recover_pending_pr_checks.py の純粋関数を stdlib unittest で検証する (#6609)。

gh は呼ばない (evaluate_pr / filter_candidate_prs / required_checks_satisfied /
has_any_required_check / latest_dispatch_run は pure function)。
"""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # scripts/ を import path に追加

from recover_pending_pr_checks import (  # noqa: E402
    STAGE2_LABEL,
    Action,
    evaluate_pr,
    filter_candidate_prs,
    has_any_required_check,
    has_stage2_label,
    head_push_time,
    latest_dispatch_run,
    required_checks_satisfied,
)

NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


def _rollup(**conclusions):
    """{"validate": "SUCCESS", "unit-tests": "FAILURE"} 形式から statusCheckRollup を作る。"""
    return [{"name": name, "conclusion": conclusion} for name, conclusion in conclusions.items()]


def _run(created_minutes_ago, event="workflow_dispatch", name="Unit Tests", head_sha="deadbeef"):
    created = NOW - dt.timedelta(minutes=created_minutes_ago)
    return {
        "event": event, "name": name, "head_sha": head_sha,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


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


class LatestDispatchRunTests(unittest.TestCase):

    def test_matches_head_sha_and_event(self):
        runs = [_run(20, head_sha="abc"), _run(10, head_sha="xyz")]
        self.assertIsNone(latest_dispatch_run(runs, "does-not-exist"))
        found = latest_dispatch_run(runs, "abc")
        self.assertIsNotNone(found)
        self.assertEqual(found["head_sha"], "abc")

    def test_ignores_non_dispatch_events(self):
        runs = [_run(10, event="pull_request", head_sha="abc")]
        self.assertIsNone(latest_dispatch_run(runs, "abc"))

    def test_ignores_unrelated_workflow_names(self):
        runs = [_run(10, name="Some Other Workflow", head_sha="abc")]
        self.assertIsNone(latest_dispatch_run(runs, "abc"))

    def test_picks_most_recent_when_multiple(self):
        older = _run(30, head_sha="abc")
        newer = _run(5, head_sha="abc")
        found = latest_dispatch_run([older, newer], "abc")
        self.assertEqual(found["created_at"], newer["created_at"])


class HeadPushTimeTests(unittest.TestCase):

    def test_uses_last_commit_committed_date(self):
        pr = {"commits": [
            {"committedDate": "2026-09-08T11:00:00Z"},
            {"committedDate": "2026-09-08T11:55:00Z"},
        ]}
        self.assertEqual(head_push_time(pr), dt.datetime(2026, 9, 8, 11, 55, tzinfo=dt.timezone.utc))

    def test_no_commits_returns_none(self):
        self.assertIsNone(head_push_time({"commits": []}))
        self.assertIsNone(head_push_time({}))


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
    """段1・段2の状態遷移をすべて網羅する。"""

    def _eval(self, **kw):
        base = dict(
            required_satisfied=False,
            has_any_check=False,
            dispatch_run=None,
            stage2_done=False,
            push_time=NOW - dt.timedelta(minutes=10),
            now=NOW,
        )
        base.update(kw)
        return evaluate_pr(**base)

    # --- 段1 (workflow_dispatch) ---------------------------------------

    def test_below_push_threshold_is_noop(self):
        """閾値未満は対象外。"""
        action = self._eval(push_time=NOW - dt.timedelta(minutes=2))
        self.assertEqual(action, Action("noop"))

    def test_push_threshold_boundary_is_actionable(self):
        action = self._eval(push_time=NOW - dt.timedelta(minutes=5))
        self.assertEqual(action, Action("dispatch"))

    def test_already_has_required_check_is_noop(self):
        """既に checks がある PR は対象外 (pull_request が発火済み = 配送漏れではない)。"""
        action = self._eval(has_any_check=True)
        self.assertEqual(action, Action("noop"))

    def test_unknown_push_time_is_noop(self):
        action = self._eval(push_time=None)
        self.assertEqual(action, Action("noop"))

    def test_eligible_after_threshold_dispatches(self):
        action = self._eval(push_time=NOW - dt.timedelta(minutes=6))
        self.assertEqual(action, Action("dispatch"))

    def test_already_dispatched_sha_does_not_dispatch_again(self):
        """既に workflow_dispatch 済みの SHA は対象外 (二重ディスパッチ禁止)。"""
        dispatch_run = _run(3)  # 3分前に dispatch 済み、まだ tick 未経過
        action = self._eval(dispatch_run=dispatch_run)
        self.assertEqual(action, Action("wait"))

    # --- 段2 (close→reopen) ---------------------------------------------

    def test_dispatch_not_enough_elapsed_waits(self):
        dispatch_run = _run(14)  # tick=15分未満
        action = self._eval(dispatch_run=dispatch_run, tick_minutes=15)
        self.assertEqual(action, Action("wait"))

    def test_dispatch_tick_elapsed_and_still_unsatisfied_advances_to_stage2(self):
        """段2に進む条件: 1 tick 経過してもまだ required check が満たされない。"""
        dispatch_run = _run(15)
        action = self._eval(dispatch_run=dispatch_run, tick_minutes=15)
        self.assertEqual(action, Action("close_reopen"))

    def test_dispatch_tick_elapsed_but_satisfied_meanwhile_is_recovered(self):
        dispatch_run = _run(20)
        action = self._eval(dispatch_run=dispatch_run, required_satisfied=True)
        self.assertEqual(action, Action("recovered_workflow_dispatch"))

    def test_stage2_done_and_now_satisfied_is_recovered_close_reopen(self):
        action = self._eval(stage2_done=True, required_satisfied=True)
        self.assertEqual(action, Action("recovered_close_reopen"))

    def test_stage2_done_and_still_unsatisfied_gives_up(self):
        """段2を1回しかやらない: stage2 済みでまだ満たされなければ recovered_by=none で停止。"""
        dispatch_run = _run(999)  # 段1の情報が残っていても stage2_done が優先される
        action = self._eval(stage2_done=True, dispatch_run=dispatch_run)
        self.assertEqual(action, Action("recovered_none"))

    def test_stage2_done_never_returns_close_reopen_again(self):
        for dispatch_run in (None, _run(999)):
            action = self._eval(stage2_done=True, dispatch_run=dispatch_run)
            self.assertNotEqual(action.kind, "close_reopen")

    # --- recovered_by の3値がそれぞれ出ること -----------------------------

    def test_all_three_recovered_by_values_are_reachable(self):
        kinds = {
            self._eval(dispatch_run=_run(20), required_satisfied=True).kind,
            self._eval(stage2_done=True, required_satisfied=True).kind,
            self._eval(stage2_done=True, required_satisfied=False).kind,
        }
        self.assertEqual(
            kinds,
            {"recovered_workflow_dispatch", "recovered_close_reopen", "recovered_none"},
        )

    def test_satisfied_without_any_intervention_history_is_noop(self):
        """通常配送 (pull_request が普通に発火) で満たされた場合は復旧対象ではない。"""
        action = self._eval(required_satisfied=True)
        self.assertEqual(action, Action("noop"))


if __name__ == "__main__":
    unittest.main()
