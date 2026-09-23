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

## 2026-09-21: dirty PR (base が古い) を close→reopen 対象から除外する (#7912)

「checks が 1 件も無い PR」には性質が違う 2 パターンがある:

| 型 | 原因 | close→reopen |
|---|---|---|
| A: 配送不発 (#6609) | GitHub 側で `pull_request` が配送されない | **治る** (本来の対象) |
| B: dirty (#7892) | base が古く GitHub がマージコミットを作れない | **原理的に絶対治らない** |

これまでは区別せず両方に close→reopen を試みていたため、B に対しても
1 PR につき 1 回しか無い close→reopen 枠 (`ci-recovery-reopened` ラベルで
冪等化) を無駄に消費し、その後 `recovered_by=none` に落ちていた (#7853)。

`mergeable == "CONFLICTING"` を専用の action kind (`dirty`) として扱い、
close→reopen を消費せずに「base を取り込めば直る」という別の warning を出す。
`mergeable` は GitHub 側で非同期に計算されるため `UNKNOWN` が返る窓がある
(#7853 は closed 後の取得で `UNKNOWN` だった)。`UNKNOWN` は dirty と誤判定
せず次回 run に判定を持ち越す (close→reopen もまだ消費しない — この窓で
誤って枠を焼くのが今回の本題)。

## 2026-09-21: UNKNOWN 先送りの可視化 / dirty 文言 / summary 矛盾 (#7919, #7915 レビュー残件)

上の `UNKNOWN` 先送りは `noop` に落ちていたため、run ログにも summary にも
一切痕跡が残らなかった。`deferred_unknown` という専用 kind に切り出し、
summary 行にカウンタを足した。さらに push からの経過時間が
`push_threshold_minutes × UNKNOWN_ESCALATE_MULTIPLIER` を超えてなお
`UNKNOWN` のままなら (= GitHub の非同期計算が異常に長引いている)
`Action.escalate=True` にして `::warning::` へ格上げする。

close→reopen 済み (`stage2_done`) の dirty PR は `evaluate_pr` の分岐順の都合上
(`stage2_done` チェックが `mergeable` チェックより先) 引き続き action kind は
`recovered_none` のままだが、`recovered_none_message` で `mergeable` を見て
「close_reopen が dirty には効かない」ことが分かる文言に寄せる。

`dirty` を検出した run で `close_reopened == 0` のときに出ていた
`"no recovery action needed this run"` は summary の `dirty=%d` と矛盾する
(`recovered_none` も dirty も `deferred_unknown` の `escalate` も何らかの
`::warning::` を伴う異常系なので、それらが 0 件のときだけ「何も要らなかった」
と言うべき)。

escalate の閾値は「UNKNOWN が続いた時間」ではなく「push からの経過」で測る
(run をまたぐ状態を持たないため)。cron は `*/15` だが、実際の起動間隔は
GitHub 側の間引きで 2〜5 時間空く (2026-09-17〜21 の run 一覧で実測)。
したがって push から 30 分以上経った PR は、UNKNOWN を初めて観測した run で
即 escalate しうる。main が頻繁に動くこのリポジトリでは dirty PR の
`mergeable` が main の更新ごとに UNKNOWN に戻るので、escalate の警告文は
「詰まっている」と断定せず、観測事実 (push から何分で UNKNOWN) だけを書く。
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

# push_threshold_minutes の何倍、経過してなお mergeable=UNKNOWN のままなら
# 「GitHub 側の非同期計算が異常に長引いている」とみなして ::warning:: に
# 格上げするか (#7919)。6 倍 = デフォルト設定で 30 分。cron は `*/15` だが実際の
# 起動間隔は 2〜5 時間空くので「2 サイクル分 UNKNOWN が続いた」の意味にはならない
# (モジュール docstring 参照)。
UNKNOWN_ESCALATE_MULTIPLIER = 6

PR_FIELDS = (
    "number,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,"
    "statusCheckRollup,labels,mergeable"
)

MERGEABLE_CONFLICTING = "CONFLICTING"
MERGEABLE_UNKNOWN = "UNKNOWN"


def emit_warning_annotation(message: str) -> None:
    """GitHub Actions の ANNOTATIONS 欄に出す workflow command。

    #7892: logger.warning は stderr に出るだけで、run 一覧・run 詳細の
    ANNOTATIONS 欄には出ない。「close→reopen も尽きて人手が要る」という
    重要な状態が6時間以上どこにも表示されず見落とされた反省から、この
    シグナルだけは stdout に `::warning::` として明示的に出す。
    """
    print(f"::warning::{message}")


def recovered_none_message(pr_number: int, mergeable: Optional[str]) -> str:
    """`recovered_none` (close→reopen 済みで復旧不可) の警告文を作る。

    #7919: `evaluate_pr` は `stage2_done` を `mergeable` より先に見るため、
    close→reopen 済みの dirty PR (#7853 の型) も action kind としては
    `recovered_none` のまま区別できない。だが「close→reopen という手段が
    dirty には原理的に効かない」ことは分かっているので、文言だけ dirty 側に
    寄せて読み手の誤解 (再度 close→reopen すれば直るかも、という期待) を防ぐ。
    """
    if mergeable == MERGEABLE_CONFLICTING:
        return (
            f"pr={pr_number} recovered_by=none "
            "(dirty; base is stale so close_reopen cannot fix this; needs rebase or re-run)"
        )
    return (
        f"pr={pr_number} recovered_by=none "
        "(close_reopen exhausted; needs human)"
    )


def dirty_message(pr_number: int) -> str:
    return (
        f"pr={pr_number} action=dirty "
        "(base is stale, mergeable=CONFLICTING; needs rebase or re-run, not close/reopen)"
    )


def deferred_unknown_escalated_message(pr_number: int,
                                       minutes_since_push: Optional[int] = None) -> str:
    # 「詰まっている」と断定しない: 観測は 1 run 分だけで、main の更新直後なら
    # dirty PR も一時的に UNKNOWN に戻る (モジュール docstring 参照)。
    age = f"{minutes_since_push} min" if minutes_since_push is not None else "well past threshold"
    return (
        f"pr={pr_number} action=deferred_unknown "
        f"(no required checks and mergeable=UNKNOWN {age} after push; "
        "GitHub may still be computing it, will re-evaluate next run)"
    )


@dataclass(frozen=True)
class Action:
    kind: str  # noop | close_reopen | recovered_close_reopen | recovered_none | dirty | deferred_unknown
    # deferred_unknown 専用: push から十分経ってもなお UNKNOWN で、GitHub の
    # 非同期計算が異常に長引いていそうなときに True (#7919)。他の kind では未使用。
    escalate: bool = False


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
    mergeable: Optional[str] = None,
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

    # ここから close→reopen を検討する分岐 (#7912)。
    # base が古く GitHub がマージコミットを作れない (dirty) PR は close→reopen
    # しても原理的に直らないので、1 回きりの枠を消費する前に弾く。
    if mergeable == MERGEABLE_CONFLICTING:
        return Action("dirty")
    if mergeable == MERGEABLE_UNKNOWN:
        # GitHub 側の非同期計算がまだ終わっていない。dirty と誤判定しないために
        # close→reopen は消費せず、次回 run で判定し直す (#7853)。ただし noop に
        # 落とすと先送りが summary にもログにも一切現れず永久に気づけない
        # (#7919) ので、専用 kind にして可視化する。push からの経過が
        # 閾値の UNKNOWN_ESCALATE_MULTIPLIER 倍を超えているなら、GitHub 側の
        # 計算が異常に長引いているとみなして ::warning:: に格上げする。
        escalate = now - push_time >= dt.timedelta(
            minutes=push_threshold_minutes * UNKNOWN_ESCALATE_MULTIPLIER
        )
        return Action("deferred_unknown", escalate=escalate)
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


def reopen_env() -> Optional[Dict[str, str]]:
    """close→reopen に使う環境変数を返す (#8092)。

    GITHUB_TOKEN で reopen しても GitHub の再帰防止で `pull_request` が発火せず、
    required check は付かない (導入以来の回復実績 0 件: #7853 / #8061)。
    ``REOPEN_GH_TOKEN`` (App token) があればそれを ``GH_TOKEN`` として渡す。
    無いとき (手元実行など) は現在の環境のまま動かし、効かない可能性を警告に残す。
    """
    token = os.environ.get("REOPEN_GH_TOKEN", "")
    if not token:
        emit_warning_annotation(
            "REOPEN_GH_TOKEN unset: close→reopen uses the ambient token "
            "(GITHUB_TOKEN reopen does not trigger pull_request, #8092)")
        return None
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    return env


def close_reopen_pr(repo: str, pr_number: int) -> None:
    env = reopen_env()
    subprocess.run(["gh", "pr", "close", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True, env=env)
    subprocess.run(["gh", "pr", "reopen", str(pr_number), "-R", repo],
                    check=True, capture_output=True, text=True, env=env)
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
    dirty = 0
    deferred_unknown = 0
    deferred_unknown_escalated = 0

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
            mergeable=pr.get("mergeable"),
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
            message = recovered_none_message(pr["number"], pr.get("mergeable"))
            logger.warning(message)
            emit_warning_annotation(message)
            recovered["none"] += 1
        elif action.kind == "dirty":
            message = dirty_message(pr["number"])
            logger.warning(message)
            emit_warning_annotation(message)
            dirty += 1
        elif action.kind == "deferred_unknown":
            logger.info("pr=%s action=deferred_unknown head=%s", pr["number"], head_sha)
            if action.escalate:
                minutes = (int((now - push_time).total_seconds() // 60)
                           if push_time is not None else None)
                message = deferred_unknown_escalated_message(pr["number"], minutes)
                logger.warning(message)
                emit_warning_annotation(message)
                deferred_unknown_escalated += 1
            deferred_unknown += 1
        else:
            logger.debug("pr=%s action=noop", pr["number"])

    logger.info(
        "summary: candidates=%d close_reopened=%d "
        "recovered_close_reopen=%d recovered_none=%d dirty=%d deferred_unknown=%d",
        len(prs), close_reopened,
        recovered["close_reopen"], recovered["none"], dirty, deferred_unknown,
    )
    # recovered_none / dirty / deferred_unknown(escalate 済み) はどれも
    # ::warning:: を伴う異常系なので、close_reopened と併せてこの 4 つが全て 0 の
    # ときだけ「何もしなかった」と言ってよい (#7919: dirty>0 でもこの行が出て
    # summary と矛盾していた。recovered_none>0 でも同じ矛盾が 2026-09-20 の
    # run 35536800101 / 35543469464 で実際に出ていた)。
    if (close_reopened == 0 and recovered["none"] == 0 and dirty == 0
            and deferred_unknown_escalated == 0):
        logger.info("no recovery action needed this run")

    return 0


if __name__ == "__main__":
    sys.exit(main())
