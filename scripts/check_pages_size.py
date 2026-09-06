"""check_pages_size.py

#6415: 待機系 GitLab Pages の 1 GiB 上限に**サイズ監視が無い**のを埋める。

## なぜ要るか

配信元は NAS に移った (#6205) が、待機系として GitLab Pages を残すことが
確定しているため、その約 1.0 GiB は生きた制約であり続ける。

- 2026-08-28、配信物が上限を超えて `pages:deploy` が落ち、**本番が約 19 時間
  凍った** (#6204 / #6205)
- 2026-08-31 時点で 644,608 KB / 成長 4.1 MB/日 → 余裕は約 97 日
- **NAS が生きている限り本番は止まらない。効くのは NAS が倒れて待機系に
  切り替わった時**。つまり「障害時に保険が使えない」という形でしか表に出ない
- 監視 51 (配信鮮度) / 52 (アセット) / 53 (オリジン failover) は**どれも
  サイズでは鳴らない**。`navi-switch` のサニティゲートは NAS 側の前後比較

## 何を測るか

**展開後の実サイズ**でなければ意味がない。GitLab の上限は展開後に効くのに、
jobs API の artifacts (archive) は zip 圧縮後で、2026-09-06 実測では展開
665MB に対し archive 220MB と 3 倍違う。Pages API (`GET /projects/:id/pages`)
の deployments はサイズを持たない (同日実測)。

そこで `.gitlab-ci.yml` の `pages` ジョブが `du -sb public` の結果を

    PAGES_ARTIFACT_BYTES=<n>

の 1 行としてジョブログに出し、本 script が**最新の成功 `pages` ジョブの
trace からその行を読む**。測っているのは配信物そのもので、GitLab が上限判定に
使う値と同じ単位。

## 落とさない

閾値を超えても CI は落とさず、issue を 1 本 upsert するだけにする
(#6415 の設計判断)。**落とすと待機系の更新が止まり、いざ倒したときに
古い配信物しか無いという逆効果になる。**

## 使いかた

    GITLAB_TOKEN=<PAT> REPO=owner/name python scripts/check_pages_size.py
    GITLAB_TOKEN=<PAT> python scripts/check_pages_size.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

try:  # package 実行と素実行の両対応
    from scripts._marker_issue import verified_matches
except ImportError:  # pragma: no cover
    from _marker_issue import verified_matches

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_pages_size")

MARKER = "pages-size-monitor"
LABELS = "infra,todo"

DEFAULT_PROJECT = "omocha/navi"
DEFAULT_API = "https://gitlab.com/api/v4"
DEFAULT_JOB_NAME = "pages"
DEFAULT_SCAN_JOBS = 60
DEFAULT_TIMEOUT = 30.0

# GitLab Pages の上限。2026-08-28 の実測エラーは
# `artifacts for pages are too large: 1059523450` (= 1,059,523,450 bytes) で
# 落ちているので、**1 GiB ちょうどではなく「1 GiB を少し超えたあたり」**が
# 実際の境界。安全側に 1 GiB (1,073,741,824) を上限として扱う。
PAGES_LIMIT_BYTES = 1024 ** 3

# 上限の何割で鳴らすか。「張り付いてから気づく」形にしないための二段。
#   warn  … 余裕が 2 割を切った。削減 or 待機系だけ noindex 除外 (#6415 の 2) の検討開始
#   alert … 余裕が 1 割。この時点で成長 4.1MB/日なら残り約 26 日
DEFAULT_WARN_RATIO = 0.80
DEFAULT_ALERT_RATIO = 0.90

SIZE_LINE_RE = re.compile(r"PAGES_ARTIFACT_BYTES=(\d+)")


class ApiError(Exception):
    """GitLab API を叩けなかった / 期待した形で返らなかった。"""


def _api(path: str, token: str, *, api: str = DEFAULT_API,
         timeout: float = DEFAULT_TIMEOUT, raw: bool = False) -> Any:
    req = urllib.request.Request(api + path, headers={"PRIVATE-TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("GET {}: {}".format(path, e)) from e
    if raw:
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ApiError("GET {}: JSON ではない応答".format(path)) from e


def parse_size_from_trace(trace: str) -> Optional[int]:
    """ジョブログから `PAGES_ARTIFACT_BYTES=<n>` を読む。

    **最後の一致を採る。** GitLab の trace には実行したコマンド自体のエコーも
    残るため、先頭を採ると値ではなくコマンド行を掴みうる。実際の値は常に後ろ。
    """
    found = SIZE_LINE_RE.findall(trace or "")
    return int(found[-1]) if found else None


def latest_measurement(project: str, token: str, *,
                       job_name: str = DEFAULT_JOB_NAME,
                       scan: int = DEFAULT_SCAN_JOBS,
                       api: str = DEFAULT_API,
                       fetch=None) -> Optional[Dict[str, Any]]:
    """直近の成功 `pages` ジョブのうち、サイズ行を持つ最初のものを返す。

    サイズ行を持たないジョブ (計測を入れる前のもの) は**飛ばす**。1 件も
    無ければ None を返し、呼び出し側は「まだ測れていない」として扱う
    — **0 と混ぜない** (#5941 と同じ型の穴を作らない)。
    """
    if fetch is None:
        def fetch(path, raw=False):
            return _api(path, token, api=api, raw=raw)
    enc = quote(project, safe="")
    jobs = fetch("/projects/{}/jobs?scope[]=success&per_page={}".format(enc, scan))
    if not isinstance(jobs, list):
        raise ApiError("jobs API が list を返さない")
    for job in jobs:
        if job.get("name") != job_name:
            continue
        trace = fetch("/projects/{}/jobs/{}/trace".format(enc, job.get("id")), raw=True)
        size = parse_size_from_trace(trace)
        if size is None:
            logger.info("job %s にサイズ行が無い — 次の成功ジョブを見る", job.get("id"))
            continue
        return {
            "job_id": job.get("id"),
            "bytes": size,
            "finished_at": job.get("finished_at"),
            "sha": (job.get("commit") or {}).get("id"),
        }
    return None


def classify(size_bytes: int, *, limit: int = PAGES_LIMIT_BYTES,
             warn_ratio: float = DEFAULT_WARN_RATIO,
             alert_ratio: float = DEFAULT_ALERT_RATIO) -> str:
    ratio = size_bytes / limit
    if ratio >= alert_ratio:
        return "alert"
    if ratio >= warn_ratio:
        return "warn"
    return "ok"


def _mib(n: int) -> str:
    return "{:,.1f} MiB".format(n / 1024 / 1024)


def _pct(size_bytes: int, limit: int) -> float:
    return size_bytes / limit * 100


def render_body(m: Dict[str, Any], status: str, *, limit: int = PAGES_LIMIT_BYTES,
                warn_ratio: float = DEFAULT_WARN_RATIO,
                alert_ratio: float = DEFAULT_ALERT_RATIO,
                now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    size = m["bytes"]
    sha = m.get("sha") or "-"
    parts: List[str] = [
        "<!-- {} -->".format(MARKER),
        "",
        "待機系 GitLab Pages の配信物が上限の **{:.1f}%** です ({} UTC 時点)。".format(
            _pct(size, limit), now.strftime("%Y-%m-%d %H:%M")),
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        "| 配信物 | {} ({:,} bytes) |".format(_mib(size), size),
        "| 上限 | {} |".format(_mib(limit)),
        "| 余裕 | {} |".format(_mib(limit - size)),
        "| 判定 | `{}` (warn {:.0%} / alert {:.0%}) |".format(
            status, warn_ratio, alert_ratio),
        "| 測定元 | GitLab job `{}` ({}) |".format(
            m.get("job_id"), m.get("finished_at") or "-"),
        "| commit | `{}` |".format(sha[:12]),
        "",
        "## 効きかた",
        "",
        "**本番 (NAS) はこれでは止まりません。** 効くのは NAS が倒れて待機系へ"
        "切り替わったときで、上限を超えていると `pages:deploy` が落ち、"
        "**待機系には古い配信物しか無い**状態で保険を使うことになります。"
        "2026-08-28 に本番が約 19 時間凍ったのはこの上限です (#6204 / #6205)。",
        "",
        "## 見かた",
        "",
        "- `warn` — 余裕が 2 割を切りました。削減 (#6415 の 3) か、"
        "**待機系だけ noindex term を落とす** (#6415 の 2) の検討を始める頃合いです",
        "- `alert` — 余裕が 1 割です。成長 4.1 MB/日なら残り 1 か月を切ります",
        "",
        "## この issue の扱い",
        "",
        "- マーカー `<!-- {} -->` で同一 open issue を特定し、body を毎回更新します".format(MARKER),
        "- 閾値を下回れば自動 close します。**CI は落としません** "
        "(落とすと待機系の更新が止まり、いざ倒したときにより古い配信物しか残らない)",
        "",
        "Refs #6415, #6205, #6204",
    ]
    return "\n".join(parts)


# --- Issue 操作 -------------------------------------------------------------

def _gh(args: List[str]) -> str:
    import subprocess
    res = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return res.stdout


def get_open_issue(repo: str) -> Optional[int]:
    query = 'repo:{} is:issue is:open in:body "{}"'.format(repo, MARKER)
    out = _gh(["api", "-X", "GET", "search/issues", "-f", "q={}".format(query),
               "-f", "per_page=10"])
    items = json.loads(out).get("items", [])
    # #6622: 検索結果は候補でしかない。マーカーコメントの実体で検証してから採用する
    matches = verified_matches(items, MARKER)
    return matches[0]["number"] if matches else None


def create_issue(repo: str, title: str, body: str) -> str:
    return _gh(["issue", "create", "-R", repo, "--title", title,
                "--label", LABELS, "--body", body]).strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    return _gh(["issue", "edit", str(number), "-R", repo,
                "--title", title, "--body", body]).strip()


def close_issue(repo: str, number: int, comment: str) -> None:
    _gh(["issue", "close", str(number), "-R", repo, "--comment", comment])


def title_for(size_bytes: int, limit: int) -> str:
    return "[infra] 待機系 GitLab Pages の配信物が上限の {:.0f}% ({})".format(
        _pct(size_bytes, limit), _mib(size_bytes))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--api", default=DEFAULT_API)
    p.add_argument("--job-name", default=DEFAULT_JOB_NAME)
    p.add_argument("--scan-jobs", type=int, default=DEFAULT_SCAN_JOBS)
    p.add_argument("--limit-bytes", type=int, default=PAGES_LIMIT_BYTES)
    p.add_argument("--warn-ratio", type=float, default=DEFAULT_WARN_RATIO)
    p.add_argument("--alert-ratio", type=float, default=DEFAULT_ALERT_RATIO)
    p.add_argument("--dry-run", action="store_true",
                   help="issue を触らず判定と body だけ出す")
    args = p.parse_args(argv)

    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        logger.error("missing $GITLAB_TOKEN")
        return 2

    try:
        m = latest_measurement(args.project, token, job_name=args.job_name,
                               scan=args.scan_jobs, api=args.api)
    except ApiError as e:
        # 測れないことと「小さい」ことを混ぜない。監視自体の故障として非ゼロで返す
        logger.error("GitLab API: %s", e)
        return 3
    if m is None:
        logger.warning("直近 %d 件の成功 %s ジョブにサイズ行が無い "
                       "(.gitlab-ci.yml の計測がまだ回っていない可能性)",
                       args.scan_jobs, args.job_name)
        return 0

    status = classify(m["bytes"], limit=args.limit_bytes,
                      warn_ratio=args.warn_ratio, alert_ratio=args.alert_ratio)
    logger.info("pages artifact = %s (%.1f%% of limit) -> %s",
                _mib(m["bytes"]), _pct(m["bytes"], args.limit_bytes), status)

    body = render_body(m, status, limit=args.limit_bytes,
                       warn_ratio=args.warn_ratio, alert_ratio=args.alert_ratio)
    if args.dry_run:
        print(body if status != "ok" else "ok: {} / {}".format(
            _mib(m["bytes"]), _mib(args.limit_bytes)))
        return 0

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    number = get_open_issue(args.repo)
    if status == "ok":
        if number is not None:
            close_issue(args.repo, number,
                        "配信物が閾値を下回りました ({} / 上限の {:.1f}%)。".format(
                            _mib(m["bytes"]), _pct(m["bytes"], args.limit_bytes)))
            logger.info("closed #%s", number)
        else:
            logger.info("under threshold; nothing to do")
        return 0

    print("::warning::待機系 GitLab Pages の配信物が上限の {:.1f}% ({})".format(
        _pct(m["bytes"], args.limit_bytes), _mib(m["bytes"])))
    title = title_for(m["bytes"], args.limit_bytes)
    if number is None:
        logger.info("created %s", create_issue(args.repo, title, body))
    else:
        logger.info("updated %s", update_issue(args.repo, number, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
