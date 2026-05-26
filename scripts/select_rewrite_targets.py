#!/usr/bin/env python3
"""Select articles to rewrite, ordered by quality score asc + slug date asc.

Used by .github/workflows/12-rewrite-idle-fill.yml to fill idle Jules slots
with rewrites of low-quality / old-prompt articles when new-ASIN fetch supply
is low (Issue #812).

Priority key (lower = higher priority):
1. ``total_score`` from ``data/articles/<slug>.quality.json`` ascending.
   Sidecar missing → treated as -1 (= top priority). A missing quality.json
   usually means the article was generated before quality_gate sidecar output
   landed, i.e. an early-prompt article that benefits most from a rewrite.
2. slug date ascending (older articles first within the same score).

Excludes ASINs that are already being processed: jules-lock branches + open PR
titles. The exclude-file is produced by the workflow with a `gh api` call and
passed in here as a flat text file.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import Iterable

_ASIN_RE = re.compile(r"(B0[A-Z0-9]{8})")
_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(B0[A-Z0-9]{8})$")
_SIDECAR_SUFFIXES = (".quality.json", ".enrichment.json", ".seo.json")


def _load_quality(slug: str, base_dir: str) -> tuple[int | None, bool | None]:
    p = os.path.join(base_dir, f"{slug}.quality.json")
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        score = d.get("total_score")
        passed = d.get("passed")
        if not isinstance(score, (int, float)):
            score = None
        if not isinstance(passed, bool):
            passed = None
        return score, passed
    except (OSError, json.JSONDecodeError):
        return None, None


def collect_candidates(articles_dir: str) -> list[dict]:
    """Return primary article records as {slug, asin, date, score, passed}.

    Skips sidecar JSONs and any file whose slug doesn't match YYYY-MM-DD-ASIN.
    """
    out: list[dict] = []
    seen_asin: set[str] = set()
    for path in sorted(glob.glob(os.path.join(articles_dir, "*.json"))):
        base = os.path.basename(path)
        if base.endswith(_SIDECAR_SUFFIXES):
            continue
        slug = base[:-5]
        m = _SLUG_RE.match(slug)
        if not m:
            continue
        date, asin = m.group(1), m.group(2)
        # If two article files reference the same ASIN (shouldn't happen but
        # be defensive: a stale rewrite + fresh one could overlap), keep the
        # older one — the rewrite is meant to replace it.
        if asin in seen_asin:
            continue
        seen_asin.add(asin)
        score, passed = _load_quality(slug, articles_dir)
        out.append({
            "slug": slug,
            "asin": asin,
            "date": date,
            "score": score,
            "passed": passed,
        })
    return out


def _read_exclude(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            for m in _ASIN_RE.finditer(line):
                out.add(m.group(1))
    return out


def select(
    candidates: Iterable[dict],
    excluded: set[str],
    limit: int,
) -> list[dict]:
    """Sort by (score asc with None→-1, date asc) and return up to ``limit``.

    None-score articles come first because a missing quality.json sidecar is
    a stronger signal of "old prompt revision" than any numeric score.
    """
    def key(c: dict) -> tuple[float, str]:
        score = c.get("score")
        if score is None:
            return (-1.0, c.get("date", ""))
        return (float(score), c.get("date", ""))

    available = [c for c in candidates if c["asin"] not in excluded]
    available.sort(key=key)
    return available[: max(limit, 0)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument(
        "--exclude-file",
        default="",
        help=(
            "Path to a text file with excluded ASINs (one per line, or any text "
            "containing B0[A-Z0-9]{8}). Caller populates this from open PR titles "
            "+ jules-lock/<ASIN> branches."
        ),
    )
    ap.add_argument(
        "--out",
        default="",
        help="Write CSV (one ASIN per line) to this path. Default: stdout.",
    )
    args = ap.parse_args()

    candidates = collect_candidates(args.articles_dir)
    excluded = _read_exclude(args.exclude_file)
    picked = select(candidates, excluded, args.limit)

    body = "\n".join(c["asin"] for c in picked)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body + ("\n" if body else ""))
    else:
        if body:
            print(body)

    print(
        f"[select_rewrite_targets] candidates={len(candidates)} "
        f"excluded={len(excluded)} picked={len(picked)} limit={args.limit}",
        file=sys.stderr,
    )
    for c in picked:
        print(
            f"  -> {c['asin']} slug={c['slug']} "
            f"score={c['score']} passed={c['passed']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
