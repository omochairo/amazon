#!/usr/bin/env python3
"""Apply rewrite targets to the Jules pool (Issue #812 / #2711 redesign).

For each target ASIN:
1. Read ``data/raw/per_asin/<ASIN>/amazon.json`` (single-item snapshot written
   by fetch_amazon --competitors-only) and prepend its ``item`` into
   ``data/raw/amazon.json`` items[]. This makes the ASIN visible to
   03-invoke-jules pick-asin candidates.
2. Write a rewrite-request marker ``data/rewrite_queue/<ASIN>.json`` recording
   the current body's slug. pick-asin keeps the ASIN eligible while a marker is
   present and no newer body exists (see ``rewrite_queue.eligible_rewrite_asins``).

Issue #2711 redesign ("replace, don't pre-delete"): this step NO LONGER deletes
the existing body. The old flow deleted the body up front and hoped
03-invoke-jules would regenerate it; any silently-failed regeneration (Jules
quota, amazon.json daily overwrite, zero-defer #1600, shuffle starvation) then
left the page permanently 404 (~338 bodies lost). Now the body survives until
``rewrite_queue.cleanup_completed`` removes it -- and that only fires once a
newer body has actually landed. A failed regeneration therefore never orphans
the page.

Idempotent: ASINs already in amazon.json items[] are skipped (no duplicate
prepend); markers re-written for the same ASIN keep their original timestamp.
Missing files are warned and skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import rewrite_queue

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")


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


def write_markers(targets: list[str], articles_dir: str, queue_dir: str) -> int:
    """Write a rewrite-request marker per target ASIN. Returns count written.

    The marker records the ASIN's current (newest) body slug as ``old_slug`` so
    cleanup can later remove exactly that body once a newer one lands. An ASIN
    with no existing body is still marked (old_slug=None): it is simply an
    uncovered ASIN that pick-asin would pick anyway, and the harmless marker is
    cleared on the next cleanup pass.
    """
    written = 0
    for asin in targets:
        old_slug = rewrite_queue.newest_body_slug(asin, articles_dir)
        rewrite_queue.write_marker(asin, old_slug or "", queue_dir)
        print(f"MARK: {asin} old_slug={old_slug}")
        written += 1
    return written


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
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--queue-dir", default=rewrite_queue.QUEUE_DIR)
    args = ap.parse_args()
    targets = _parse_targets(args.asins)
    if not targets:
        print("No targets; nothing to do.")
        return 0
    injected = inject_targets(args.pool, args.per_asin_root, targets)
    marked = write_markers(targets, args.articles_dir, args.queue_dir)
    print(f"[apply_rewrite_targets] injected={injected} marked={marked} targets={len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
