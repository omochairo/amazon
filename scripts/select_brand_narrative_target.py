"""Pick one brand for Jules narrative generation.

#731 epic — companion to .github/workflows/14-brand-narrative.yml.

Selection rules (priority order):
1. brand must have `count >= 3` and all narrative-required stats set
   (avg_age_min_months / avg_ivs_100 / lowest_price / tier / top_3_series).
2. top_3_series must contain at least one entry that is NOT a verbatim copy of
   the brand name (= the brand exposes a real series taxonomy, not just itself).
   Otherwise the narrative would be content-thin.
3. Skip brand if hugo/content/brands/<brand>/_index.md exists with
   `auto_generated: false` (manual narrative — never overwrite).
4. Among the rest, prefer brands with NO _index.md yet (new coverage > refresh).
5. Tiebreak by `last_generated_at` ascending (oldest first), then alphabetically.

Output: JSON to stdout with the chosen brand's stats, OR exits with code 2
(no eligible brand) which the workflow treats as a no-op.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Ensure UTF-8 stdout on Windows runners (the workflow runs on ubuntu so this
# is a no-op there, but local Windows dev/test would otherwise emit cp932).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


REQUIRED_FIELDS = (
    "avg_age_min_months",
    "avg_ivs_100",
    "lowest_price",
    "tier",
    "top_3_series",
    "representative_asin",
    "representative_image",
)


@dataclass
class Candidate:
    name: str
    stats: dict
    has_index: bool
    last_generated_at: Optional[str]


def _read_frontmatter(path: Path) -> dict:
    """Minimal YAML frontmatter reader (we only need title / auto_generated /
    last_generated_at — no nested structures, no lists). Avoids adding a yaml
    runtime dep to the cron environment."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    out: dict = {}
    for line in block.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip('"').strip("'")
        if val.lower() == "true":
            out[key.strip()] = True
        elif val.lower() == "false":
            out[key.strip()] = False
        else:
            out[key.strip()] = val
    return out


def _is_informational(top_3_series: list[str], brand: str) -> bool:
    non_self = [s for s in top_3_series if s.strip().lower() != brand.strip().lower()]
    return len(non_self) >= 1


def select(brand_hub: dict, brands_dir: Path) -> Optional[Candidate]:
    candidates: list[Candidate] = []
    skipped_reasons: list[str] = []
    for name, stats in brand_hub.get("brands", {}).items():
        if stats.get("count", 0) < 3:
            continue
        if not all(f in stats for f in REQUIRED_FIELDS):
            skipped_reasons.append(f"{name}: missing required stats")
            continue
        top3 = stats.get("top_3_series", [])
        if not isinstance(top3, list) or len(top3) < 3:
            skipped_reasons.append(f"{name}: top_3_series < 3")
            continue
        if not _is_informational(top3, name):
            skipped_reasons.append(f"{name}: top_3_series all self-references")
            continue
        index_path = brands_dir / name / "_index.md"
        fm = _read_frontmatter(index_path)
        if fm.get("auto_generated") is False:
            skipped_reasons.append(f"{name}: manual narrative (auto_generated:false)")
            continue
        candidates.append(
            Candidate(
                name=name,
                stats=stats,
                has_index=bool(fm),
                last_generated_at=fm.get("last_generated_at") if fm else None,
            )
        )

    if not candidates:
        print(f"No eligible brands. Skipped: {len(skipped_reasons)}", file=sys.stderr)
        for r in skipped_reasons[:10]:
            print(f"  - {r}", file=sys.stderr)
        return None

    # New coverage first (no index), then oldest refresh, then name asc.
    candidates.sort(
        key=lambda c: (
            c.has_index,
            c.last_generated_at or "",
            c.name,
        )
    )
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand-hub", default="hugo/data/brand_hub.json")
    p.add_argument("--brands-dir", default="hugo/content/brands")
    args = p.parse_args(argv)

    brand_hub = json.loads(Path(args.brand_hub).read_text(encoding="utf-8"))
    chosen = select(brand_hub, Path(args.brands_dir))
    if chosen is None:
        return 2

    top3 = chosen.stats["top_3_series"]
    out = {
        "brand": chosen.name,
        "count": chosen.stats["count"],
        "tier": chosen.stats["tier"],
        "avg_ivs_100": chosen.stats["avg_ivs_100"],
        "avg_age_min_months": chosen.stats["avg_age_min_months"],
        "lowest_price": chosen.stats["lowest_price"],
        "top_3_series_csv": ", ".join(top3),
        "representative_asin": chosen.stats["representative_asin"],
        "representative_image": chosen.stats["representative_image"],
        "is_refresh": chosen.has_index,
        "previous_last_generated_at": chosen.last_generated_at,
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
