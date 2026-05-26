#!/usr/bin/env python3
"""Apply rewrite targets to the Jules pool (Issue #812 Phase 1).

For each target ASIN:
1. Read ``data/raw/per_asin/<ASIN>/amazon.json`` (single-item snapshot written
   by fetch_amazon --competitors-only) and prepend its ``item`` into
   ``data/raw/amazon.json`` items[]. This makes the ASIN visible to
   03-invoke-jules pick-asin candidates.
2. Delete the existing primary article + sidecar files so the pick-asin
   "already-covered" filter no longer excludes the ASIN.

Idempotent: ASINs already in amazon.json items[] are skipped (no duplicate
prepend). Missing files are warned and skipped.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_SIDECAR_GLOBS = (
    "data/articles/*-{asin}.json",
    "data/articles/*-{asin}.quality.json",
    "data/articles/*-{asin}.enrichment.json",
    "data/articles/*-{asin}.seo.json",
)


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: failed to read {path}: {e}", file=sys.stderr)
        return None


def inject_targets(pool_path: str, per_asin_root: str, targets: list[str]) -> int:
    """Prepend per_asin snapshots into the Jules pool. Returns count injected."""
    pool = _load_json(pool_path)
    if pool is None:
        raise SystemExit(f"Cannot read pool {pool_path}")
    items = pool.setdefault("items", [])
    existing = {it.get("asin") for it in items if isinstance(it, dict)}
    injected = 0
    for asin in targets:
        if asin in existing:
            print(f"SKIP: {asin} already in pool items")
            continue
        snap_path = os.path.join(per_asin_root, asin, "amazon.json")
        snap = _load_json(snap_path)
        if not snap:
            print(f"WARN: no per_asin snapshot for {asin}; skipping inject")
            continue
        item = snap.get("item")
        if not isinstance(item, dict) or not item.get("asin"):
            print(f"WARN: malformed snapshot for {asin}; skipping")
            continue
        # Tag the source so the rewrite path is grep-able in the pool history.
        if not item.get("source"):
            item["source"] = "Rewrite (idle-fill)"
        items.insert(0, item)
        existing.add(asin)
        injected += 1
        print(f"INJECT: {asin}")
    with open(pool_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)
    return injected


def delete_article_files(articles_glob_base: str, targets: list[str]) -> int:
    """Remove primary + sidecar JSON files for each target ASIN. Returns count."""
    removed = 0
    for asin in targets:
        for pattern_tpl in _SIDECAR_GLOBS:
            pattern = pattern_tpl.format(asin=asin)
            # articles_glob_base lets tests redirect the root; production uses cwd.
            full_pattern = os.path.join(articles_glob_base, pattern) if articles_glob_base else pattern
            for path in glob.glob(full_pattern):
                try:
                    os.remove(path)
                    print(f"RM: {path}")
                    removed += 1
                except OSError as e:
                    print(f"WARN: failed to remove {path}: {e}", file=sys.stderr)
    return removed


def _parse_targets(csv: str) -> list[str]:
    out: list[str] = []
    for raw in csv.split(","):
        a = raw.strip()
        if not a:
            continue
        if not _ASIN_RE.match(a):
            raise SystemExit(f"Invalid ASIN format: {a!r}")
        out.append(a)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asins", required=True, help="CSV of target ASINs")
    ap.add_argument("--pool", default="data/raw/amazon.json")
    ap.add_argument("--per-asin-root", default="data/raw/per_asin")
    ap.add_argument(
        "--articles-base",
        default="",
        help="Optional path prefix prepended to the deletion glob patterns "
             "(default empty = relative to cwd).",
    )
    args = ap.parse_args()
    targets = _parse_targets(args.asins)
    if not targets:
        print("No targets; nothing to do.")
        return 0
    injected = inject_targets(args.pool, args.per_asin_root, targets)
    removed = delete_article_files(args.articles_base, targets)
    print(f"[apply_rewrite_targets] injected={injected} removed={removed} targets={len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
