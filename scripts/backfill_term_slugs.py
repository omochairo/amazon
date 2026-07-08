"""#2817 Phase 1: data/term_slugs.yaml の初回一括生成。

data/articles/*.json の tags 全種 + data/brand_taxonomy.yaml の canonical 全件を
走査し、未登録の JP 用語に scripts/term_slug.TermSlugMap でスラッグを割り当てる。

一度実行して data/term_slugs.yaml をコミットした後は、新規タグの追加は
ensure_tag_slugs.py (04-validate-article-pr.yml から呼び出し、Phase 4) が担う。
本スクリプトは初回バックフィル専用 (再実行しても既存エントリは変更しない = 冪等)。

使い方:
    python scripts/backfill_term_slugs.py [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import yaml

from term_slug import TermSlugMap

_SIDECAR_SUFFIXES = (".enrichment.json", ".seo.json", ".quality.json")


def _collect_tags(articles_dir: str = "data/articles") -> set:
    tags: set[str] = set()
    for f in sorted(glob.glob(f"{articles_dir}/*.json")):
        if f.endswith(_SIDECAR_SUFFIXES):
            continue
        try:
            data = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for t in data.get("tags", []) or []:
            t = str(t).rstrip(". ").strip()
            if t:
                tags.add(t)
    return tags


def _collect_brand_canonicals(taxonomy_path: str = "data/brand_taxonomy.yaml") -> set:
    path = pathlib.Path(taxonomy_path)
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        entry["canonical"]
        for entry in data.get("brands", []) or []
        if entry.get("canonical")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--brand-taxonomy", default="data/brand_taxonomy.yaml")
    ap.add_argument("--out", default="data/term_slugs.yaml")
    ap.add_argument("--dry-run", action="store_true", help="スラッグを計算するがファイルには書き込まない")
    args = ap.parse_args()

    tags = _collect_tags(args.articles_dir)
    brands = _collect_brand_canonicals(args.brand_taxonomy)
    terms = sorted(tags | brands)
    print(f"tags={len(tags)} brand_canonicals={len(brands)} union={len(terms)}", file=sys.stderr)

    slug_map = TermSlugMap(path=args.out, brand_taxonomy_path=args.brand_taxonomy)
    new_count = 0
    for term in terms:
        if slug_map.get(term) is None:
            new_count += 1
        slug_map.get_or_create(term)

    print(f"newly assigned: {new_count} / total mapped: {len(terms)}", file=sys.stderr)

    if args.dry_run:
        print("--dry-run: not writing output file.", file=sys.stderr)
        return 0

    slug_map.save()
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
