"""ランナー不在で落ちた配信パイプラインを 1 回だけやり直す (2026-11-12)。

## なぜ要るか

配信のビルド (`build-site`) は自宅の K8 の gitlab-runner で走る。自宅の回線が
切れるとランナーは GitLab に届かず、その間に来た push のパイプラインは
`stuck_pending_no_matching_runners` で打ち切られる (2026-11-12 実測: 06:16 JST の
パイプラインが 08:06 JST に落ちた)。

**回線が戻っても、落ちたパイプラインをやり直す仕組みが無い。** 次の push が来れば
そのパイプラインが配信するので昼間は目立たないが、夜や休日のように push が
途切れる時間帯は、配信が止まったままになる。`check_delivery_freshness.py` は 8h
で鳴らすが、鳴らすだけで直さない。

## 何をやり直すか

`main` の**最新の**パイプラインが、次をすべて満たすときだけ retry API を叩く。

1. 状態が `failed` で、作られてから `--max-age-hours` (既定 24h) 以内
2. 落ちた配信ジョブ (`DELIVERY_JOBS`) の失敗理由が**すべて**ランナー側のもの
   (`RETRYABLE_REASONS`)。`script_failure` などビルド自体の失敗が 1 つでも
   混ざっていたら触らない —— やり直しても同じ所で落ちるだけ
3. そのジョブのやり直しがまだ `--max-retries` (既定 1) 回に達していない
4. いまオンラインで止められていないランナーが 1 台以上ある。居ないうちに
   やり直すと、また 2 時間待って同じ理由で落ちるだけなので、待つ

新しい push が来ていれば最新はそちらになるので、古いパイプラインを掘り起こす
ことはない。状態は GitLab 側 (やり直し前のジョブも API に残る) にあるので、
こちらに持たない。

**最新の 1 本だけを見る**のは意図的。push 由来だけに絞ると、手動で走らせた新しい
パイプラインが成功したあとに古い push のパイプラインをやり直し、古い sha で本番を
上書きしうる (navi-switch は「新しい方へ」を知らない)。2026-11-12 時点で GitLab の
定期実行 (pipeline schedules) は 5 本とも止めてあり、起動元は全部 push。

`stuck_or_timeout_failure` にはビルド自体の時間切れも含まれるが、1 回だけなので
見逃すより安い。2 回目に落ちたら `exhausted` で人に回す。

## 監視とは分ける

`gitlab_pipeline_status.py` の docstring にあるとおり、異常の**検知**は外形監視
(`check_delivery_freshness.py`) が GitLab に依存せずにやる。この script は
**復旧の手**であって検知ではないので、GitLab に依存してよい。GitLab の API が
読めないときは何もせず非 0 で終わる (黙って緑にしない — #4793)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retry_stuck_pipeline")

DEFAULT_PROJECT = "omocha/navi"
DEFAULT_REF = "main"
DEFAULT_API = "https://gitlab.com/api/v4"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_MAX_RETRIES = 1

# gitlab_pipeline_status.DELIVERY_JOBS と同じ集合 (配信に効くジョブ)。
DELIVERY_JOBS = frozenset({"build-site", "deploy-nas", "pages", "pages:deploy", "cf-purge"})

# ランナー側の都合で落ちたことを示す failure_reason。ビルドの中身は無実。
# `stuck_pending_no_matching_runners` は 2026-11-12 に実際に返った値。
RETRYABLE_REASONS = frozenset({
    "stuck_pending_no_matching_runners",
    "stuck_or_timeout_failure",
    "runner_system_failure",
    "scheduler_failure",
})


class ApiError(Exception):
    pass


class AuthError(ApiError):
    """トークンが無効・権限不足。黙って緑にしない (失効は赤で知りたい・#4793)。"""


def parse_iso(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def decide(pipeline: Optional[Dict[str, Any]], jobs: List[Dict[str, Any]],
           online_runners: int, now: dt.datetime, *,
           max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
           max_retries: int = DEFAULT_MAX_RETRIES) -> Dict[str, Any]:
    """やり直すかを決める純関数。

    ``jobs`` は ``include_retried=true`` で取った一覧 (やり直し前の試行も含む)。
    返す ``action`` は none / retry / wait / exhausted / real_failure。
    """
    row: Dict[str, Any] = {"action": "none", "detail": "", "pipeline_id": None,
                           "sha": None, "jobs": []}
    if not pipeline:
        row["detail"] = "main のパイプラインが無い"
        return row
    row["pipeline_id"] = pipeline.get("id")
    row["sha"] = (pipeline.get("sha") or "")[:10]
    status = pipeline.get("status")
    if status != "failed":
        row["detail"] = "最新のパイプラインは {}".format(status)
        return row
    created = parse_iso(pipeline.get("created_at"))
    if created is None or (now - created).total_seconds() / 3600.0 > max_age_hours:
        row["detail"] = "失敗が {:.0f}h より古い。人が見る".format(max_age_hours)
        return row

    # 名前ごとに試行をまとめる。最後の試行 (id が最大) が今の状態。
    attempts: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        if job.get("name") in DELIVERY_JOBS:
            attempts.setdefault(job["name"], []).append(job)
    failed = []
    for name, tries in attempts.items():
        last = max(tries, key=lambda j: j.get("id") or 0)
        if last.get("status") == "failed" and not last.get("allow_failure"):
            failed.append((name, last, len(tries)))
    row["jobs"] = [{"name": n, "reason": j.get("failure_reason"), "attempts": k}
                   for n, j, k in failed]
    if not failed:
        row["detail"] = "落ちた配信ジョブが無い (配信以外のジョブの失敗)"
        return row

    real = [n for n, j, _ in failed if j.get("failure_reason") not in RETRYABLE_REASONS]
    if real:
        row["action"] = "real_failure"
        row["detail"] = "ビルド自体の失敗を含む ({})。やり直さない".format(", ".join(sorted(real)))
        return row
    if any(k > max_retries for _, _, k in failed):
        row["action"] = "exhausted"
        row["detail"] = "すでに {} 回やり直して、また落ちている。人が見る".format(max_retries)
        return row
    if online_runners < 1:
        row["action"] = "wait"
        row["detail"] = "オンラインのランナーが居ない。戻るのを待つ"
        return row
    row["action"] = "retry"
    row["detail"] = "ランナー不在で落ちている ({})。やり直す".format(
        ", ".join(sorted("{}={}".format(n, j.get("failure_reason")) for n, j, _ in failed)))
    return row


# --- GitLab API ---------------------------------------------------------------

def api(method: str, path: str, params: Optional[Dict[str, str]] = None, *,
        project: str, token: str, base: str = DEFAULT_API,
        timeout: float = DEFAULT_TIMEOUT) -> Any:
    import requests  # 遅延 import: テストは差し替えるので requests 不要

    url = "{}/projects/{}{}".format(base, quote(project, safe=""), path)
    try:
        resp = requests.request(method, url, params=params, timeout=timeout,
                                headers={"PRIVATE-TOKEN": token})
    except Exception as exc:
        raise ApiError("{}: {}".format(type(exc).__name__, exc)) from exc
    if resp.status_code in (401, 403):
        raise AuthError("HTTP {} — GITLAB_TOKEN が無効か、api スコープが足りない".format(
            resp.status_code))
    if resp.status_code not in (200, 201):
        raise ApiError("{} {} → HTTP {}: {}".format(method, path, resp.status_code,
                                                   resp.text[:200]))
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError("{} {} が JSON を返さない: {}".format(method, path, exc)) from exc


def run(call: Callable[..., Any], ref: str, now: dt.datetime, *, dry_run: bool,
        max_age_hours: float, max_retries: int) -> Dict[str, Any]:
    pipelines = call("GET", "/pipelines", {"ref": ref, "per_page": "1"})
    if not isinstance(pipelines, list):
        raise ApiError("pipelines API が list を返さない")
    pipeline = pipelines[0] if pipelines else None
    jobs: List[Dict[str, Any]] = []
    online = 0
    if pipeline and pipeline.get("status") == "failed":
        jobs = call("GET", "/pipelines/{}/jobs".format(pipeline["id"]),
                    {"per_page": "100", "include_retried": "true"})
        runners = call("GET", "/runners", {"status": "online", "per_page": "100"})
        online = sum(1 for r in runners if not r.get("paused"))
    row = decide(pipeline, jobs, online, now,
                 max_age_hours=max_age_hours, max_retries=max_retries)
    if row["action"] == "retry":
        if dry_run:
            row["detail"] += " [dry-run のため叩いていない]"
        else:
            call("POST", "/pipelines/{}/retry".format(pipeline["id"]), None)
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--ref", default=DEFAULT_REF)
    p.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    token = os.environ.get("GITLAB_TOKEN") or ""
    if not token:
        # 黙って緑にしない (#4793)。失効した日に復旧の手が黙って死ぬ
        logger.error("GITLAB_TOKEN が要ります")
        return 2

    def call(method: str, path: str, params: Optional[Dict[str, str]]) -> Any:
        return api(method, path, params, project=args.project, token=token)

    try:
        row = run(call, args.ref, dt.datetime.now(dt.timezone.utc),
                  dry_run=args.dry_run, max_age_hours=args.max_age_hours,
                  max_retries=args.max_retries)
    except AuthError as e:
        logger.error("GitLab API の認証に失敗: %s", e)
        return 1
    except ApiError as e:
        # GitLab 側の一時障害で毎時の監視ジョブを赤にしない (上の監視ステップとは別の
        # 結果だが、ジョブが赤だと監視の異常と見分けにくい)。次の run でまた見る。
        print("::warning::GitLab API が読めないのでやり直しを見送った: {}".format(e))
        return 0
    logger.info("action=%s pipeline=%s sha=%s %s", row["action"], row["pipeline_id"],
                row["sha"], row["detail"])
    if row["action"] in ("exhausted", "real_failure"):
        # 直せない失敗。鳴らすのは 51 の Issue の役目なので、ここは注記だけ
        print("::warning::配信パイプラインが落ちています: {}".format(row["detail"]))
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
