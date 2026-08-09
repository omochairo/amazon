#!/usr/bin/env python3
"""マージ/クローズ済み PR の残骸ブランチを、少しずつ削除する (#4765 follow-up)。

背景:
  repo 設定 `delete_branch_on_merge` が長く false だったため、squash merge された
  PR のブランチが消えずに 4,200 本以上たまった (feat 512 / tag-slugs 411 /
  sns-engagement-publish 248 …)。設定は 2026-08-09 に true にしたので今後は増えないが、
  既存分は残る。fetch と `gh pr list` が重くなる以外の実害は無いので、急いで消す
  理由も無い。

なぜ一括削除しないか:
  CLAUDE.md の GitHub API バースト禁止規律。2026-06-25 のアカウント凍結は自動化
  経由の大量 API 発行と時期が一致しており (理由非開示のまま手動レビューで解除)、
  4,000 本を一気に DELETE するのは同じパターンを踏む。1 日 --limit 本ずつ、削除
  ごとに --sleep 秒空けて回す。

削除する条件 (全部を満たすときだけ):
  1. そのブランチを head に持つ PR があり、state が MERGED か CLOSED
     → jules-lock/* (Jules の運用ロック) や gitlab/* は PR を持たないので、この
       条件だけで構造的に除外される
  2. open な PR の head になっていない
  3. 最終コミットから --min-age-days 日以上経っている
  4. PROTECT_PREFIXES に一致しない (保険。条件 1 と二重に効かせる)

削除したブランチは PR ページの "Restore branch" から復元できる。

副作用:
  - origin のブランチ削除のみ。main / data / 記事には触れない。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Sequence, Set, Tuple

DEFAULT_REPO = "omochairo/amazon"
# 条件 1 で既に除外されるが、事故の余地を残さないため明示する。
# jules-lock/* を消すと Jules Lock Cleanup (#1495 系) の排他が壊れる。
PROTECT_PREFIXES = ("jules-lock/", "gitlab/", "main", "master")


def select_targets(
    branches: Sequence[Tuple[str, datetime]],
    deletable: Set[str],
    open_heads: Set[str],
    cutoff: datetime,
    limit: int,
    protect_prefixes: Sequence[str] = PROTECT_PREFIXES,
) -> List[Tuple[str, datetime]]:
    """削除対象を古い順に最大 `limit` 本返す。

    `branches` は (ブランチ名, 最終コミット日時) を古い順に並べたもの。
    `deletable` は「PR が MERGED/CLOSED の head 名」、`open_heads` は open PR の head 名。
    """
    out: List[Tuple[str, datetime]] = []
    for name, ts in branches:
        if name.startswith(tuple(protect_prefixes)):
            continue
        if name in open_heads or name not in deletable:
            continue
        if ts > cutoff:
            continue
        out.append((name, ts))
        if len(out) >= limit:
            break
    return out


def _sh(args: List[str], check: bool = True) -> str:
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise SystemExit("command failed: {}\n{}".format(" ".join(args), p.stderr))
    return p.stdout


def load_pr_index(repo: str) -> Tuple[Set[str], Set[str]]:
    """(PR が merged/closed の head 名, open PR の head 名) を返す。

    open な PR の head は merged/closed 側から必ず引く。同じブランチ名が再利用され、
    古い PR が merged・新しい PR が open ということが起こりうるため。
    """
    raw = _sh(["gh", "pr", "list", "-R", repo, "--state", "all",
               "--limit", "6000", "--json", "headRefName,state"])
    prs: List[Dict[str, str]] = json.loads(raw)
    done = {p["headRefName"] for p in prs if p["state"] in ("MERGED", "CLOSED")}
    open_ = {p["headRefName"] for p in prs if p["state"] == "OPEN"}
    print("PR {} 件 (merged/closed={}, open={})".format(len(prs), len(done), len(open_)))
    return done - open_, open_


def remote_branches() -> List[Tuple[str, datetime]]:
    """(ブランチ名, 最終コミット日時) を古い順で返す。"""
    out = _sh(["git", "for-each-ref", "--sort=committerdate",
               "--format=%(refname:short)%09%(committerdate:iso8601-strict)",
               "refs/remotes/origin"])
    rows: List[Tuple[str, datetime]] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, ts = line.split("\t", 1)
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        if name in ("HEAD", "main"):
            continue
        try:
            rows.append((name, datetime.fromisoformat(ts)))
        except ValueError:
            continue
    return rows


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--limit", type=int, default=200, help="1 回に削除する上限 (default 200)")
    ap.add_argument("--sleep", type=float, default=1.0, help="削除間隔の秒数 (default 1.0)")
    ap.add_argument("--min-age-days", type=int, default=7,
                    help="この日数より新しいブランチは触らない (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="削除せず対象だけ出す")
    args = ap.parse_args(list(argv) if argv is not None else None)

    _sh(["git", "fetch", "--prune", "--quiet", "origin"])
    deletable, open_heads = load_pr_index(args.repo)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)
    batch = select_targets(remote_branches(), deletable, open_heads, cutoff, args.limit)

    if not batch:
        print("::notice::削除対象なし。ブランチ整理は完了しています。")
        return 0
    print("今回の対象 {} 本 ({} 〜 {})".format(
        len(batch), batch[0][1].date(), batch[-1][1].date()))

    if args.dry_run:
        for name, ts in batch[:20]:
            print("  {}  {}".format(ts.date(), name))
        if len(batch) > 20:
            print("  … 他 {} 本".format(len(batch) - 20))
        return 0

    ok = fail = 0
    for i, (name, _) in enumerate(batch, 1):
        p = subprocess.run(
            ["gh", "api", "-X", "DELETE",
             "repos/{}/git/refs/heads/{}".format(args.repo, name)],
            capture_output=True, text=True, encoding="utf-8")
        if p.returncode == 0:
            ok += 1
        else:
            fail += 1
            print("  [fail] {}: {}".format(name, p.stderr.strip()[:160]), file=sys.stderr)
        if i % 50 == 0:
            print("  {}/{} (ok={} fail={})".format(i, len(batch), ok, fail), flush=True)
        time.sleep(args.sleep)

    print("::notice::削除 {} 本 / 失敗 {} 本".format(ok, fail))
    # 失敗は握りつぶさない。全滅は権限や API 側の異常なので run を赤くする。
    return 1 if ok == 0 and fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
