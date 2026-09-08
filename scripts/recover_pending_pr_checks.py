"""recover_pending_pr_checks.py

#6609: `pull_request` イベントが GitHub 側で配送されず、required check
(`validate` / `unit-tests`) が 1 件も付かないまま PR が永久 pending になる事故への
自動復旧。#6606 で bot push・human push の両方が未配送になり、3 コミット目でようやく
発火する事故が起きた。トリガー定義側 (paths/branches フィルタ) には原因が無く、
GitHub 側の配送問題 (再現条件未特定)。

## 2段構えにした理由

前提検証で「`workflow_dispatch` run が required check として認識されるか」について、
このリポジトリの履歴に **直接矛盾する記録**が見つかった:

- PR #285 (2026-05-19, 実測): 手動 dispatch した check_run が branch protection に
  required check として受理された、と明記。
- `.github/workflows/01-fetch-products.yml` (現行, PR #400 由来): workflow_dispatch
  run が required check に算入されないことが auto-merge stuck の根本原因だった、
  と明記。

どちらの記録が正しいかを過去データだけで決着させる方法が無い
(「pull_request が発火しない状態」を意図的に再現できない)。よって議論を先に
決着させず、**どちらが正しくても正しく動く**ように 2 段構えにして本番に測らせる。

- 段1: `workflow_dispatch` で 44/04 を再実行する (安価・副作用が少ない)。
- 段2: 段1のディスパッチから 1 tick (既定 15 分) 経っても required check が
  満たされないままなら、PR を close→reopen して `pull_request` (reopened) を
  再発火させる (段1より確実だが通知等の副作用があるので段1が効かなかったときだけ)。
- どちらで回復したか (`recovered_by=workflow_dispatch` / `close_reopen` / `none`)
  を run ログに残す。これが「どちらの記録が正しかったか」の実地での答えになる。

## 判定に使うフィールド

「required check として満たされているか」は `gh pr list --json statusCheckRollup`
の各エントリの `name` と `conclusion` で判定する (`conclusion == "SUCCESS"`)。
check-run の**存在**ではなく**required context ごとの合否**を見ること — ここが
今回の論点そのものなので、存在チェックで代用しない。

「head コミットの push 時刻」は GitHub API に直接の相当フィールドが無いため、
head commit の `committedDate` を代理指標として使う ([推] push とほぼ同時に
コミットされる自動化 PR がほとんどなので、5分閾値・15分 cron の粒度では
十分な近似)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recover_pending_pr_checks")

REQUIRED_CONTEXTS = ("validate", "unit-tests")
RECOVERY_WORKFLOW_FILES = ("44-unit-tests.yml", "04-validate-article-pr.yml")
# gh api /actions/runs が返す `name` は workflow の `name:` 定義値 (job id ではない)。
RECOVERY_WORKFLOW_NAMES = ("Unit Tests", "Validate Article PR")
STAGE2_LABEL = "ci-recovery-reopened"

DEFAULT_PUSH_THRESHOLD_MINUTES = 5
DEFAULT_TICK_MINUTES = 15

PR_FIELDS = (
    "number,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,"
    "statusCheckRollup,commits,labels"
)


@dataclass(frozen=True)
class Action:
    kind: str  # noop | wait | dispatch | close_reopen |
               # recovered_workflow_dispatch | recovered_close_reopen | recovered_none


def _parse_ts(value: str) -> dt.datetime:
    """GitHub の ISO 8601 (`2026-08-04T01:23:45Z`) を aware datetime にする。"""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def required_checks_satisfied(status_check_rollup: List[Dict[str, Any]],
                               required: tuple = REQUIRED_CONTEXTS) -> bool:
    """required の全 context が SUCCESS で報告されているか。"""
    ok = {
        row.get("name") for row in status_check_rollup
        if row.get("conclusion") == "SUCCESS"
    }
    return all(name in ok for name in required)


def has_any_required_check(status_check_rollup: List[Dict[str, Any]],
                            required: tuple = REQUIRED_CONTEXTS) -> bool:
    """required のいずれかが (合否問わず) 既に報告されているか。

    報告済み = `pull_request` が少なくとも一度は発火した、とみなせる
    (段1の対象は「一度も発火していない」PR に限る)。
    """
    return any(row.get("name") in required for row in status_check_rollup)


def filter_candidate_prs(prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """draft と fork (headRepository が別リポジトリ) を除外する。

    fork PR は `--ref` で dispatch できず (段1)、close→reopen も fork 側の
    権限が絡むため対象外にする (段2)。
    """
    return [pr for pr in prs if not pr.get("isDraft") and not pr.get("isCrossRepository")]


def head_push_time(pr: Dict[str, Any]) -> Optional[dt.datetime]:
    commits = pr.get("commits") or []
    if not commits:
        return None
    return _parse_ts(commits[-1]["committedDate"])


def has_stage2_label(pr: Dict[str, Any]) -> bool:
    return any(l.get("name") == STAGE2_LABEL for l in (pr.get("labels") or []))


def latest_dispatch_run(runs: List[Dict[str, Any]], head_sha: str) -> Optional[Dict[str, Any]]:
    """head_sha に対する workflow_dispatch run のうち最新のものを返す。"""
    matches = [
        r for r in runs
        if r.get("event") == "workflow_dispatch"
        and r.get("head_sha") == head_sha
        and r.get("name") in RECOVERY_WORKFLOW_NAMES
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: r["created_at"])


def evaluate_pr(
    *,
    required_satisfied: bool,
    has_any_check: bool,
    dispatch_run: Optional[Dict[str, Any]],
    stage2_done: bool,
    push_time: Optional[dt.datetime],
    now: dt.datetime,
    push_threshold_minutes: int = DEFAULT_PUSH_THRESHOLD_MINUTES,
    tick_minutes: int = DEFAULT_TICK_MINUTES,
) -> Action:
    """1 PR ぶんの状態から、次に取るアクションを決める純粋関数。"""
    if required_satisfied:
        if stage2_done:
            return Action("recovered_close_reopen")
        if dispatch_run is not None:
            return Action("recovered_workflow_dispatch")
        return Action("noop")  # 通常配送で満たされた。復旧の出番ではない。

    # ここから required check 未充足
    if stage2_done:
        # 段2まで使い切ってなお満たされない。無限ループ禁止 — これ以上は何もしない。
        return Action("recovered_none")
    if dispatch_run is not None:
        elapsed = now - _parse_ts(dispatch_run["created_at"])
        if elapsed >= dt.timedelta(minutes=tick_minutes):
            return Action("close_reopen")
        return Action("wait")  # 段1がまだ結果を出せる時間の余地がある
    if has_any_check:
        # pull_request は少なくとも一度発火している = 配送漏れではない
        # (validate/unit-tests 自体の legitimate failure。復旧対象外)。
        return Action("noop")
    if push_time is None:
        return Action("noop")  # push 時刻が取れない = 判定不能。安全側に倒す。
    if now - push_time < dt.timedelta(minutes=push_threshold_minutes):
        return Action("noop")  # まだ実行中かもしれない。誤爆させない。
    return Action("dispatch")


# --- I/O 層 (gh CLI 経由。単体テストでは触らない) -----------------------------

def list_candidate_prs(repo: str) -> List[Dict[str, Any]]:
    res = subprocess.run(
        ["gh", "pr", "list", "-R", repo, "--state", "open", "--base", "main",
         "--limit", "100", "--json", PR_FIELDS],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        # check=True のままだと CalledProcessError が stderr を飲み込み、run ログに
        # 「exit status 1」しか残らない (2026-09-08 の初回 dry_run で実際に踏んだ)。
        # gh 自身のメッセージを必ず表に出す。
        logger.error("gh pr list failed (exit %s): %s", res.returncode,
                     (res.stderr or "").strip() or "(no stderr)")
        raise SystemExit(1)
    prs = json.loads(res.stdout or "[]")
    return filter_candidate_prs(prs)


def list_dispatch_runs(repo: str, head_sha: str) -> List[Dict[str, Any]]:
    # `-X GET` は省略しないこと。`gh api` は `-f` を渡すとデフォルトの HTTP method を
    # POST に切り替える仕様があり、明示しないと GET 専用のこのエンドポイントが
    # 404 を返す (実機で踏んで確認済み)。
    res = subprocess.run(
        ["gh", "api", "-X", "GET", f"repos/{repo}/actions/runs",
         "-f", f"head_sha={head_sha}", "-f", "event=workflow_dispatch",
         "-f", "per_page=20"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(res.stdout).get("workflow_runs", [])


def dispatch_recovery_workflows(repo: str, ref: str) -> None:
    for wf in RECOVERY_WORKFLOW_FILES:
        subprocess.run(
            ["gh", "workflow", "run", wf, "-R", repo, "--ref", ref],
            check=True, capture_output=True, text=True,
        )


def close_reopen_pr(repo: str, pr_number: int) -> None:
    subprocess.run(["gh", "pr", "close", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True)
    subprocess.run(["gh", "pr", "reopen", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True)
    subprocess.run(["gh", "pr", "edit", str(pr_number), "-R", repo,
                     "--add-label", STAGE2_LABEL],
                    check=True, capture_output=True, text=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--push-threshold-minutes", type=int,
                   default=DEFAULT_PUSH_THRESHOLD_MINUTES)
    p.add_argument("--tick-minutes", type=int, default=DEFAULT_TICK_MINUTES)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    prs = list_candidate_prs(args.repo)
    now = dt.datetime.now(dt.timezone.utc)

    dispatched = 0
    close_reopened = 0
    recovered = {"workflow_dispatch": 0, "close_reopen": 0, "none": 0}

    for pr in prs:
        head_sha = pr["headRefOid"]
        rollup = pr.get("statusCheckRollup") or []
        satisfied = required_checks_satisfied(rollup)
        any_check = has_any_required_check(rollup)
        stage2_done = has_stage2_label(pr)
        push_time = head_push_time(pr)

        dispatch_run = None
        # satisfied かつ stage2_done のときは evaluate_pr が dispatch_run を
        # 見ないので API 呼び出しを省く。それ以外は判定に要るので毎回引く。
        if not (satisfied and stage2_done):
            dispatch_run = latest_dispatch_run(
                list_dispatch_runs(args.repo, head_sha), head_sha
            )

        action = evaluate_pr(
            required_satisfied=satisfied,
            has_any_check=any_check,
            dispatch_run=dispatch_run,
            stage2_done=stage2_done,
            push_time=push_time,
            now=now,
            push_threshold_minutes=args.push_threshold_minutes,
            tick_minutes=args.tick_minutes,
        )

        if action.kind == "dispatch":
            logger.info("pr=%s action=dispatch head=%s ref=%s",
                        pr["number"], head_sha, pr["headRefName"])
            if not args.dry_run:
                dispatch_recovery_workflows(args.repo, pr["headRefName"])
            dispatched += 1
        elif action.kind == "close_reopen":
            logger.info("pr=%s action=close_reopen head=%s", pr["number"], head_sha)
            if not args.dry_run:
                close_reopen_pr(args.repo, pr["number"])
            close_reopened += 1
        elif action.kind == "recovered_workflow_dispatch":
            logger.info("pr=%s recovered_by=workflow_dispatch", pr["number"])
            recovered["workflow_dispatch"] += 1
        elif action.kind == "recovered_close_reopen":
            logger.info("pr=%s recovered_by=close_reopen", pr["number"])
            recovered["close_reopen"] += 1
        elif action.kind == "recovered_none":
            logger.warning(
                "pr=%s recovered_by=none (stage2 exhausted; needs human)", pr["number"]
            )
            recovered["none"] += 1
        elif action.kind == "wait":
            logger.info("pr=%s action=wait (dispatched, tick not elapsed yet)", pr["number"])
        else:
            logger.debug("pr=%s action=noop", pr["number"])

    logger.info(
        "summary: candidates=%d dispatched=%d close_reopened=%d "
        "recovered_workflow_dispatch=%d recovered_close_reopen=%d recovered_none=%d",
        len(prs), dispatched, close_reopened,
        recovered["workflow_dispatch"], recovered["close_reopen"], recovered["none"],
    )
    if dispatched == 0 and close_reopened == 0:
        logger.info("no recovery action needed this run")

    return 0


if __name__ == "__main__":
    sys.exit(main())
