"""GitLab (omocha/navi) の配信パイプラインを手元から見る (#5260)。

## なぜ要るか

配信は 2 つの CI をまたいでいる。

    main へ push → 40-mirror-to-gitlab.yml が git push
                 → GitLab CI の pages ジョブ → navi.omcha.jp

GitHub 側の責務は ``git push`` が成功するところで終わっている。そして GitLab
プロジェクト ``omocha/navi`` は **private** なので、パイプラインの状況は ``gh``
からも匿名の API からも見えない。「マージしたのに本番が変わらない」ときに
**ビルド待ちなのか、落ちたのか、そもそも届いていないのか**を切り分ける手段が
リポジトリ内に無かった (API を叩くのは 41-pages-cert-renew のドメイン証明書
チェックだけ)。

この script は GitLab の PAT (``GITLAB_TOKEN``、40-mirror-to-gitlab.yml が使うのと
同じもの) で pipelines / jobs API を読むだけの **読み取り専用**ツール。

## 使いかた

    GITLAB_TOKEN=<PAT> python scripts/gitlab_pipeline_status.py
    GITLAB_TOKEN=<PAT> python scripts/gitlab_pipeline_status.py --sha <commit>
    GITLAB_TOKEN=<PAT> python scripts/gitlab_pipeline_status.py --json

``--sha`` は「この commit の配信は終わったか」を見るためのもの。GitHub 側で
マージした commit の SHA をそのまま渡せる (ミラーは同じ SHA を push するため)。

## 監視には使わない

異常検知は ``check_delivery_freshness.py`` (#5042) と ``check_asset_delivery.py``
(#5260) が外形から原因非依存でやる。**監視対象の CI に監視を依存させない**のが
そちらの設計判断なので、この script は人間が原因を追うときの道具に留める。
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
logger = logging.getLogger("gitlab_pipeline_status")

DEFAULT_PROJECT = "omocha/navi"
DEFAULT_REF = "main"
DEFAULT_API = "https://gitlab.com/api/v4"
DEFAULT_TIMEOUT = 30.0

# 配信に効くジョブだけを先頭に並べる。データ収集系 (fetch-data 等) が同じ ref で
# 走っていても配信の可否とは関係が無いため、見る順で優先度を表現する。
DELIVERY_JOBS = ["pages", "pages:deploy", "cf-purge"]


class ApiError(Exception):
    """GitLab API を叩けなかった / 期待した形で返らなかった。"""


def _duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _age(timestamp: Optional[str], now: dt.datetime) -> str:
    if not timestamp:
        return "-"
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    minutes = (now - parsed.astimezone(dt.timezone.utc)).total_seconds() / 60
    if minutes < 60:
        return f"{minutes:.0f}分前"
    return f"{minutes / 60:.1f}時間前"


def fetch_pipelines(get: Callable[[str, Dict[str, str]], Any], ref: str,
                    sha: Optional[str], limit: int) -> List[Dict[str, Any]]:
    params = {"ref": ref, "per_page": str(limit)}
    if sha:
        # sha 指定時は ref を外す。ミラー元のブランチ名と GitLab 側の ref が
        # 食い違っていても掴めるようにするため。
        params = {"sha": sha, "per_page": str(limit)}
    rows = get("/pipelines", params)
    if not isinstance(rows, list):
        raise ApiError(f"pipelines API が list を返さない: {type(rows).__name__}")
    return rows


def fetch_jobs(get: Callable[[str, Dict[str, str]], Any],
               pipeline_id: int) -> List[Dict[str, Any]]:
    rows = get(f"/pipelines/{pipeline_id}/jobs", {"per_page": "100"})
    if not isinstance(rows, list):
        raise ApiError(f"jobs API が list を返さない: {type(rows).__name__}")
    return rows


def summarize(pipelines: Sequence[Dict[str, Any]], jobs: Sequence[Dict[str, Any]],
              now: dt.datetime) -> Dict[str, Any]:
    """表示と --json の両方が使う 1 件の要約に畳む。"""
    latest = pipelines[0] if pipelines else None

    def job_row(job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": job.get("name", "?"),
            "stage": job.get("stage", "?"),
            "status": job.get("status", "?"),
            "duration": job.get("duration"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "web_url": job.get("web_url", ""),
        }

    rows = [job_row(j) for j in jobs]
    # 配信に効くジョブを先に、残りは元の順で。
    rows.sort(key=lambda r: (DELIVERY_JOBS.index(r["name"])
                             if r["name"] in DELIVERY_JOBS else len(DELIVERY_JOBS)))

    delivery = [r for r in rows if r["name"] in DELIVERY_JOBS]
    if not delivery:
        verdict = "配信ジョブが 1 つも無い (push 起動になっていない可能性)"
    elif any(r["status"] == "failed" for r in delivery):
        verdict = "配信ジョブが失敗している"
    elif any(r["status"] in ("running", "pending", "created", "waiting_for_resource")
             for r in delivery):
        verdict = "配信ジョブが実行中 / 待機中 (本番はまだ古い)"
    elif all(r["status"] in ("success", "skipped", "manual") for r in delivery):
        verdict = "配信ジョブは完走済み (本番に出ていなければエッジキャッシュを疑う)"
    else:
        verdict = "配信ジョブの状態が想定外"

    return {
        "pipeline": {
            "id": latest.get("id") if latest else None,
            "status": latest.get("status") if latest else None,
            "sha": (latest.get("sha") or "")[:12] if latest else None,
            "ref": latest.get("ref") if latest else None,
            "created_at": latest.get("created_at") if latest else None,
            "updated_at": latest.get("updated_at") if latest else None,
            "web_url": latest.get("web_url", "") if latest else "",
        },
        "verdict": verdict if latest else "該当するパイプラインが無い",
        "jobs": rows,
        "recent": [
            {"id": p.get("id"), "status": p.get("status"),
             "sha": (p.get("sha") or "")[:12], "created_at": p.get("created_at"),
             "age": _age(p.get("created_at"), now)}
            for p in pipelines
        ],
    }


def render(summary: Dict[str, Any], now: dt.datetime) -> str:
    pipe = summary["pipeline"]
    lines = [
        "== 最新パイプライン ==",
        f"  id       : {pipe['id']}",
        f"  status   : {pipe['status']}",
        f"  sha      : {pipe['sha']}  (ref={pipe['ref']})",
        f"  created  : {pipe['created_at']}  ({_age(pipe['created_at'], now)})",
        f"  updated  : {pipe['updated_at']}  ({_age(pipe['updated_at'], now)})",
        f"  url      : {pipe['web_url']}",
        "",
        f"  → {summary['verdict']}",
        "",
        "== ジョブ ==",
    ]
    if summary["jobs"]:
        lines.append(f"  {'ジョブ':<16} {'stage':<11} {'状態':<10} {'所要':<8}")
        for job in summary["jobs"]:
            mark = "*" if job["name"] in DELIVERY_JOBS else " "
            lines.append(f" {mark}{job['name']:<16} {job['stage']:<11} "
                         f"{job['status']:<10} {_duration(job['duration']):<8}")
    else:
        lines.append("  (なし)")

    lines += ["", "== 直近のパイプライン =="]
    for row in summary["recent"]:
        lines.append(f"  {row['id']}  {row['status']:<10} {row['sha']}  {row['age']}")
    return "\n".join(lines)


def api_get(path: str, params: Dict[str, str], *, project: str, token: str,
            api: str = DEFAULT_API, timeout: float = DEFAULT_TIMEOUT) -> Any:
    import requests  # 遅延 import: テストは get を差し替えるので requests 不要

    url = f"{api}/projects/{quote(project, safe='')}{path}"
    try:
        resp = requests.get(url, params=params, timeout=timeout,
                            headers={"PRIVATE-TOKEN": token})
    except Exception as exc:  # requests の例外階層に依存しない
        raise ApiError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code == 401:
        raise ApiError("401 — GITLAB_TOKEN が無効か、api スコープが足りない")
    if resp.status_code == 404:
        raise ApiError(f"404 — プロジェクト {project} が見えない "
                       "(private なので read_api 以上の権限が要る)")
    if resp.status_code != 200:
        raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=os.environ.get("GITLAB_PROJECT", DEFAULT_PROJECT))
    p.add_argument("--ref", default=DEFAULT_REF)
    p.add_argument("--sha", default=None,
                   help="この commit のパイプラインを見る (GitHub 側の SHA をそのまま渡せる)")
    p.add_argument("--limit", type=int, default=5, help="一覧に出す直近パイプライン数")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = p.parse_args(argv)

    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        logger.error("環境変数 GITLAB_TOKEN が要ります "
                     "(omocha/navi は private のため匿名では見えない)")
        return 2

    def get(path: str, params: Dict[str, str]) -> Any:
        return api_get(path, params, project=args.project, token=token,
                       timeout=args.timeout)

    now = dt.datetime.now(dt.timezone.utc)
    try:
        pipelines = fetch_pipelines(get, args.ref, args.sha, args.limit)
        jobs = fetch_jobs(get, pipelines[0]["id"]) if pipelines else []
    except ApiError as exc:
        logger.error("%s", exc)
        return 1

    summary = summarize(pipelines, jobs, now)
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.as_json
          else render(summary, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
