"""Aggregate per-ASIN seller frequencies and classify them against the
current TRUSTED_SELLER_PATTERNS / TRUSTED_SELLER_GENERIC_MARKERS heuristics.

Purpose (Issue #740): replace ad-hoc one-shot analysis with a repeatable
report so that future expansion of trusted-seller patterns can be driven
by real drop counts rather than guesswork.

Inputs:
  data/raw/per_asin/<ASIN>/amazon.json  (item.seller field)

Outputs:
  - stdout: human-readable summary (top-N trusted / untrusted)
  - --json <path>: machine-readable {summary, untrusted: [...], trusted: [...]}

Usage:
  python scripts/analyze_reseller_drops.py
  python scripts/analyze_reseller_drops.py --top 100 --json data/raw/reseller_drop_stats.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import sys
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fetch_amazon import is_trusted_seller  # noqa: E402


def aggregate(per_asin_dir: pathlib.Path) -> Dict:
    """Walk per_asin/<ASIN>/amazon.json and tally seller counts."""
    sellers: collections.Counter = collections.Counter()
    asin_examples: Dict[str, List[str]] = collections.defaultdict(list)
    n_total = n_empty = 0
    if not per_asin_dir.exists():
        raise FileNotFoundError(f"per_asin dir not found: {per_asin_dir}")
    for asin in os.listdir(per_asin_dir):
        p = per_asin_dir / asin / "amazon.json"
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        item = d.get("item") or {}
        s = (item.get("seller") or "").strip()
        n_total += 1
        if not s:
            n_empty += 1
            continue
        sellers[s] += 1
        if len(asin_examples[s]) < 3:
            asin_examples[s].append(asin)
    return {
        "n_total": n_total,
        "n_empty": n_empty,
        "sellers": sellers,
        "asin_examples": dict(asin_examples),
    }


def classify(stats: Dict) -> Dict:
    """Split aggregated sellers into trusted / untrusted using current rules."""
    trusted: List[Dict] = []
    untrusted: List[Dict] = []
    for seller, count in stats["sellers"].most_common():
        entry = {
            "seller": seller,
            "count": count,
            "asin_examples": stats["asin_examples"].get(seller, []),
        }
        if is_trusted_seller(seller):
            trusted.append(entry)
        else:
            untrusted.append(entry)
    return {"trusted": trusted, "untrusted": untrusted}


def build_summary(stats: Dict, cls: Dict) -> Dict:
    trusted_asins = sum(e["count"] for e in cls["trusted"])
    untrusted_asins = sum(e["count"] for e in cls["untrusted"])
    return {
        "per_asin_total": stats["n_total"],
        "empty_seller": stats["n_empty"],
        "unique_sellers": len(stats["sellers"]),
        "trusted_unique": len(cls["trusted"]),
        "trusted_asins": trusted_asins,
        "untrusted_unique": len(cls["untrusted"]),
        "untrusted_asins": untrusted_asins,
        "trust_rate_asins": (
            round(trusted_asins / (trusted_asins + untrusted_asins), 4)
            if (trusted_asins + untrusted_asins) > 0
            else 0.0
        ),
    }


def print_report(summary: Dict, cls: Dict, top: int) -> None:
    print("=== Summary ===")
    for k, v in summary.items():
        print(f"  {k:24s}: {v}")
    print()
    print(f"=== Top {top} UNTRUSTED sellers (= currently dropped) ===")
    for e in cls["untrusted"][:top]:
        ex = ",".join(e["asin_examples"])
        print(f"  {e['count']:4d}  {e['seller']}  [{ex}]")
    print()
    print(f"=== Top {min(top, 30)} TRUSTED sellers ===")
    for e in cls["trusted"][: min(top, 30)]:
        print(f"  {e['count']:4d}  {e['seller']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-asin-dir",
        default=str(ROOT / "data" / "raw" / "per_asin"),
        help="Directory containing per_asin/<ASIN>/amazon.json files",
    )
    parser.add_argument(
        "--top", type=int, default=80, help="How many sellers to print per category"
    )
    parser.add_argument(
        "--json",
        default="",
        help="Optional path to write full machine-readable JSON report",
    )
    args = parser.parse_args()

    stats = aggregate(pathlib.Path(args.per_asin_dir))
    cls = classify(stats)
    summary = build_summary(stats, cls)
    print_report(summary, cls, args.top)

    if args.json:
        out = {
            "summary": summary,
            "untrusted": cls["untrusted"],
            "trusted": cls["trusted"],
        }
        pathlib.Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nWrote JSON report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
