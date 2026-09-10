"""recover_pending_pr_checks.py

#6609: `pull_request` イベントが GitHub 側で配送されず、required check
(`validate` / `unit-tests`) が 1 件も付かないまま PR が永久 pending になる事故への
自動復旧。#6606 で bot push・human push の両方が未配送になり、3 コミット目でようやく
発火する事故が起きた。トリガー定義側 (paths/branches フィルタ) には原因が無く、
GitHub 側の配送問題 (再現条件未特定)。

## 2026-09-10: 2段構えをやめ、close→reopen 一本化にした `[実]`

当初 (#6807) は「`workflow_dispatch` run が required check として認識されるか」に
ついてリポジトリ内に矛盾する記録があったため、決着させずに2段構え
(段1: `workflow_dispatch` 再実行 → 段2: 15分待って改善しなければ close→reopen)
にして production に測らせる設計だった。

PR #6885 (#6933 で詳細分析) で答えが出た: 段1の `workflow_dispatch` run は
`conclusion: success` で完走したが、その後 **4時間半、介在イベント無しで**
`statusCheckRollup` は空のまま推移した。つまり本リポジトリのブランチ保護設定では
**`workflow_dispatch` は required check を満たさない**。段1は「常に何も解決せず
15分待つだけ」の無駄打ちであることが実地で確定したため、段1を削除して
close→reopen 直行に一本化した (#6609 のコメント参照)。

close→reopen は1 PR につき1回だけ (ラベル `ci-recovery-reopened` で冪等化)。
それでも満たされなければ `recovered_by=none` として諦める (無限ループ禁止)。
draft / fork は対象外 (fork は close→reopen に権限が絡む)。

## 判定に使うフィールド

「required check として満たされているか」は `gh pr list --json statusCheckRollup`
の各エントリの `name` と `conclusion` で判定する (`conclusion == "SUCCESS"`)。
check-run の**存在**ではなく**required context ごとの合否**を見ること。

「head コミットの push 時刻」は GitHub API に直接の相当フィールドが無いため、
head commit の committer date を代理指標として使う ([推] push とほぼ同時に
コミットされる自動化 PR がほとんどなので、5分閾値の粒度では十分な近似)。
正常系の配送は 2〜4 秒に密集しテールが無く、不発時は 20 分待っても発火しない
(#6609 の実測)。両者の差が 500〜1000 倍あるので 5 分の閾値選択に鋭敏さは無い。

## #6808 事後: `commits` を PR_FIELDS から外した理由

`gh pr list --json ...,commits --limit 100` は GraphQL のノード数上限
(500,000) に静的コスト解析で毎回引っかかり exit non-zero で落ちる (open PR が
0 件でも失敗する — 実行時のデータ量ではなく `--limit 100 × commits` の組み
合わせ自体がコスト超過と判定される)。#6809 で schedule を止血停止した。

代わりに head commit の日時は PR 単位で `gh api repos/{repo}/commits/{sha}`
から個別に取る。この呼び出しは `evaluate_pr` が実際に `push_time` を見る
条件 (required check 未充足 かつ checks 皆無 かつ close→reopen 未実施)
のときだけ行う — 満たされている PR や既に対応済みの PR まで律儀に叩くと
API 呼び出しが線形に増えるため。
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
STAGE2_LABEL = "ci-recovery-reopened"

DEFAULT_PUSH_THRESHOLD_MINUTES = 5

PR_FIELDS = (
    "number,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,"
    "statusCheckRollup,labels"
)


@dataclass(frozen=True)
class Action:
    kind: str  # noop | close_reopen | recovered_close_reopen | recovered_none


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
    (復旧対象は「一度も発火していない」PR に限る)。
    """
    return any(row.get("name") in required for row in status_check_rollup)


def filter_candidate_prs(prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """draft と fork (headRepository が別リポジトリ) を除外する。

    fork PR は close→reopen に fork 側の権限が絡むため対象外にする。
    """
    return [pr for pr in prs if not pr.get("isDraft") and not pr.get("isCrossRepository")]


def resolve_push_time(
    pr: Dict[str, Any],
    *,
    required_satisfied: bool,
    has_any_check: bool,
    stage2_done: bool,
    fetch_commit_time,
) -> Optional[dt.datetime]:
    """`evaluate_pr` が実際に push_time を見る条件でだけ commit 時刻を取りに行く。

    その条件は required_satisfied=False かつ has_any_check=False かつ
    stage2_done=False のときだけ (evaluate_pr の分岐を参照)。それ以外は
    None を渡しても結果が変わらないので、余計な `gh api` 呼び出しをしない。
    """
    if required_satisfied or has_any_check or stage2_done:
        return None
    return fetch_commit_time(pr["headRefOid"])


def _parse_commit_date_result(
    returncode: int, stdout: str, stderr: str, head_sha: str
) -> Optional[dt.datetime]:
    if returncode != 0:
        logger.warning("could not fetch commit date for %s: %s", head_sha, stderr.strip())
        return None
    value = stdout.strip()
    if not value:
        logger.warning("empty commit date for %s", head_sha)
        return None
    return _parse_ts(value)


def fetch_head_commit_committed_date(repo: str, head_sha: str) -> Optional[dt.datetime]:
    # check=True にしない: 失敗しても evaluate_pr は push_time=None を noop
    # (安全側) として扱う設計なので、ここで例外を投げて run 全体を落とす
    # 必要が無い。ただし失敗を無言にしない (#6808 の教訓) ため stderr は
    # 必ず logger.warning に出す (_parse_commit_date_result 側)。
    res = subprocess.run(
        ["gh", "api", "-X", "GET", f"repos/{repo}/commits/{head_sha}",
         "-q", ".commit.committer.date"],
        capture_output=True, text=True,
    )
    return _parse_commit_date_result(res.returncode, res.stdout, res.stderr, head_sha)


def has_stage2_label(pr: Dict[str, Any]) -> bool:
    return any(l.get("name") == STAGE2_LABEL for l in (pr.get("labels") or []))


def evaluate_pr(
    *,
    required_satisfied: bool,
    has_any_check: bool,
    stage2_done: bool,
    push_time: Optional[dt.datetime],
    now: dt.datetime,
    push_threshold_minutes: int = DEFAULT_PUSH_THRESHOLD_MINUTES,
) -> Action:
    """1 PR ぶんの状態から、次に取るアクションを決める純粋関数。"""
    if required_satisfied:
        if stage2_done:
            return Action("recovered_close_reopen")
        return Action("noop")  # 通常配送で満たされた。復旧の出番ではない。

    # ここから required check 未充足
    if stage2_done:
        # close→reopen まで使い切ってなお満たされない。無限ループ禁止 — これ以上は何もしない。
        return Action("recovered_none")
    if has_any_check:
        # pull_request は少なくとも一度発火している = 配送漏れではない
        # (validate/unit-tests 自体の legitimate failure。復旧対象外)。
        return Action("noop")
    if push_time is None:
        return Action("noop")  # push 時刻が取れない = 判定不能。安全側に倒す。
    if now - push_time < dt.timedelta(minutes=push_threshold_minutes):
        return Action("noop")  # まだ実行中かもしれない。誤爆させない。
    return Action("close_reopen")


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


def ensure_label_exists(repo: str, label: str) -> None:
    """`close_reopen_pr` が付ける完了マーカーのラベルを用意する。

    #6885: このラベルが repo に存在しないと直後の `--add-label` が exit 1 で
    失敗し、close_reopen_pr が例外で中断して stage2_done が一切記録されない。
    その結果 has_stage2_label が毎回 False のままになり、evaluate_pr は
    「まだやっていない」と誤認して close_reopen を延々と繰り返す
    (無限ループ禁止のはずの安全装置が効かない)。
    `gh label create` は既存ラベルに対して非0 exit するが、それは
    「ラベルは既にある」という望む状態そのものなので結果を見ずに無視してよい。
    """
    subprocess.run(
        ["gh", "label", "create", label, "-R", repo,
         "--color", "ededed",
         "--description", "PR Check Recovery が close→reopen を実施済み"],
        capture_output=True, text=True,
    )


def close_reopen_pr(repo: str, pr_number: int) -> None:
    subprocess.run(["gh", "pr", "close", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True)
    subprocess.run(["gh", "pr", "reopen", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True)
    ensure_label_exists(repo, STAGE2_LABEL)
    subprocess.run(["gh", "pr", "edit", str(pr_number), "-R", repo,
                     "--add-label", STAGE2_LABEL],
                    check=True, capture_output=True, text=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--push-threshold-minutes", type=int,
                   default=DEFAULT_PUSH_THRESHOLD_MINUTES)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    prs = list_candidate_prs(args.repo)
    now = dt.datetime.now(dt.timezone.utc)

    close_reopened = 0
    recovered = {"close_reopen": 0, "none": 0}

    for pr in prs:
        head_sha = pr["headRefOid"]
        rollup = pr.get("statusCheckRollup") or []
        satisfied = required_checks_satisfied(rollup)
        any_check = has_any_required_check(rollup)
        stage2_done = has_stage2_label(pr)

        push_time = resolve_push_time(
            pr,
            required_satisfied=satisfied,
            has_any_check=any_check,
            stage2_done=stage2_done,
            fetch_commit_time=lambda sha: fetch_head_commit_committed_date(args.repo, sha),
        )

        action = evaluate_pr(
            required_satisfied=satisfied,
            has_any_check=any_check,
            stage2_done=stage2_done,
            push_time=push_time,
            now=now,
            push_threshold_minutes=args.push_threshold_minutes,
        )

        if action.kind == "close_reopen":
            logger.info("pr=%s action=close_reopen head=%s", pr["number"], head_sha)
            if not args.dry_run:
                close_reopen_pr(args.repo, pr["number"])
            close_reopened += 1
        elif action.kind == "recovered_close_reopen":
            logger.info("pr=%s recovered_by=close_reopen", pr["number"])
            recovered["close_reopen"] += 1
        elif action.kind == "recovered_none":
            logger.warning(
                "pr=%s recovered_by=none (close_reopen exhausted; needs human)", pr["number"]
            )
            recovered["none"] += 1
        else:
            logger.debug("pr=%s action=noop", pr["number"])

    logger.info(
        "summary: candidates=%d close_reopened=%d "
        "recovered_close_reopen=%d recovered_none=%d",
        len(prs), close_reopened,
        recovered["close_reopen"], recovered["none"],
    )
    if close_reopened == 0:
        logger.info("no recovery action needed this run")

    return 0


if __name__ == "__main__":
    sys.exit(main())
