#!/usr/bin/env python3
"""One-off backfill for ``data/analytics/rewrite_ledger.jsonl`` (amazon-navi-brain#39 Step 0-a).

Why: ``scripts.rewrite_queue.cleanup_completed`` started recording ledger rows
only going forward. Before that, rewrites already happened for months and their
history lives only in git (a deleted ``data/articles/<old_slug>.json`` whose
ASIN survives under a newer slug). Without a backfill, ``audit_uniqueness``'s
3-way cohort split (pre_v7 / post_v7_new / post_v7_rewrite) would treat every
already-rewritten article as "brand new" until the *next* rewrite touches it.

Approach:
  1. ``git log --diff-filter=D --name-only`` over ``data/articles/*.json``
     (primary bodies only, sidecars excluded) to find every deleted slug, its
     ASIN, and the commit date it was deleted.
  2. For each ASIN with 1+ deletions, keep only the deletion with the latest
     commit date (chain history is not needed -- cohort assignment only cares
     "was this ASIN ever rewritten", see ``scripts.audit_uniqueness.cohort3_for_entry``).
  3. If that ASIN currently has a live body newer than the deleted slug
     (``scripts.rewrite_queue.newest_body_slug``), emit one ledger row
     ``{asin, old_slug, new_slug, completed_at, source: "backfill"}``.
     ASINs with no surviving body (quarantined, blocklisted, etc.) are skipped:
     there is no current article to assign a cohort to.

Safe to re-run: ``append_ledger`` only appends, and readers
(``scripts.audit_uniqueness.load_rewrite_ledger``) take the max-``completed_at``
row per asin, so re-running after new deletions land just adds more rows
without corrupting the "latest wins" read.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

from scripts.rewrite_queue import append_ledger, newest_body_slug

_HEADER_RE = re.compile(r"^@@(?P<sha>[0-9a-f]+)\|(?P<date>.+)$")
_DELETED_SLUG_RE = re.compile(r"^data/articles/(?P<slug>\d{4}-\d{2}-\d{2}-B0[A-Z0-9]{8})\.json$")


def run_git_log(articles_glob: str = "data/articles/*.json") -> str:
    """Run the git log command this backfill depends on and return its stdout."""
    out = subprocess.run(
        [
            "git", "log", "--diff-filter=D", "--name-only",
            "--pretty=format:@@%H|%aI", "--", articles_glob,
        ],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def parse_deletions(git_log_output: str) -> list[dict]:
    """Parse ``run_git_log`` output into ``[{old_slug, asin, commit_date}, ...]``.

    Pure function (no git/filesystem access) so it can be unit tested against
    synthetic output. Sidecar files (``*.quality.json`` etc.) never match
    ``_DELETED_SLUG_RE`` (it requires the bare ``<slug>.json`` filename) so
    they are skipped without special-casing.
    """
    out: list[dict] = []
    commit_date: str | None = None
    for line in git_log_output.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            commit_date = header.group("date")
            continue
        m = _DELETED_SLUG_RE.match(line.strip())
        if not m or commit_date is None:
            continue
        slug = m.group("slug")
        asin = slug[-10:]
        out.append({"old_slug": slug, "asin": asin, "commit_date": commit_date})
    return out


def latest_deletion_per_asin(deletions: list[dict]) -> dict[str, dict]:
    """Collapse multiple deletions of the same ASIN to the most recent one."""
    latest: dict[str, dict] = {}
    for d in deletions:
        cur = latest.get(d["asin"])
        if cur is None or d["commit_date"] > cur["commit_date"]:
            latest[d["asin"]] = d
    return latest


def build_ledger_records(deletions: list[dict], articles_dir: str = "data/articles") -> list[dict]:
    """Turn parsed deletions into ledger rows for ASINs that still have a live body."""
    records: list[dict] = []
    for asin, d in sorted(latest_deletion_per_asin(deletions).items()):
        new_slug = newest_body_slug(asin, articles_dir)
        if not new_slug or new_slug == d["old_slug"]:
            continue
        records.append({
            "asin": asin,
            "old_slug": d["old_slug"],
            "new_slug": new_slug,
            "completed_at": d["commit_date"],
            "source": "backfill",
        })
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--out", default="data/analytics/rewrite_ledger.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="print a summary only, don't write")
    args = ap.parse_args()

    git_log_output = run_git_log(f"{args.articles_dir}/*.json")
    deletions = parse_deletions(git_log_output)
    records = build_ledger_records(deletions, args.articles_dir)

    if args.dry_run:
        print(f"[backfill_rewrite_ledger] deletions_seen={len(deletions)} "
              f"asins_with_deletion={len(latest_deletion_per_asin(deletions))} "
              f"ledger_rows_to_write={len(records)}")
        return 0

    written = append_ledger(records, args.out)
    print(f"[backfill_rewrite_ledger] wrote {written} row(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
