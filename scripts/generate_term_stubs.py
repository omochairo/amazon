"""#2817 Phase 2: content/tags/<slug>/_index.md ・ content/brands/<slug>/_index.md
の title スタブを一括生成する。

data/term_slugs.yaml で定義済みの (JP用語, スラッグ) について、対応する物理
ディレクトリが無ければ `title: "<JP用語>"` だけの最小 _index.md を作る。
Hugo は物理ディレクトリ名 (=スラッグ) を URL に、この title を .Title/.LinkTitle
の表示名 (JP) に使う (slug:/url: front matter override は taxonomy term 一覧
ページで humanize された生スラッグが出てしまう Hugo の既知の癖があるため使わない
— 検証済み)。

既に物理ディレクトリが存在する場合は何もしない (冪等・上書きしない)。これにより
scripts/migrate_brand_dirs_to_slug.py でリネーム済みの既存 9 件のブランドハブ
(narrative付き) や、将来 Phase 4 で追加される新規タグにも安全に繰り返し実行できる。

使い方:
    python scripts/generate_term_stubs.py [--dry-run]
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backfill_term_slugs import _collect_brand_canonicals, _collect_tags  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_STUB_TEMPLATE = '---\ntitle: "{title}"\n---\n'


def _write_stub_if_missing(base_dir: pathlib.Path, slug: str, jp_title: str, dry_run: bool) -> str:
    target = base_dir / slug / "_index.md"
    if target.exists():
        return "skipped"
    if dry_run:
        return "would-create"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STUB_TEMPLATE.format(title=jp_title), encoding="utf-8")
    return "created"


def _exclude_from_taxonomy_canonicals(taxonomy_path: pathlib.Path) -> set:
    if not taxonomy_path.exists():
        return set()
    data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
    return {
        entry["canonical"]
        for entry in data.get("brands", []) or []
        if entry.get("canonical") and entry.get("exclude_from_taxonomy")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--brand-taxonomy", default="data/brand_taxonomy.yaml")
    ap.add_argument("--slugs", default="data/term_slugs.yaml")
    ap.add_argument("--tags-dir", default="hugo/content/tags")
    ap.add_argument("--brands-dir", default="hugo/content/brands")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slugs = yaml.safe_load(pathlib.Path(args.slugs).read_text(encoding="utf-8")) or {}
    tags = _collect_tags(args.articles_dir)
    brands = _collect_brand_canonicals(args.brand_taxonomy)
    excluded_brands = _exclude_from_taxonomy_canonicals(pathlib.Path(args.brand_taxonomy))

    counts = {"tags": {"created": 0, "skipped": 0, "would-create": 0}, "brands": {"created": 0, "skipped": 0, "would-create": 0}}
    missing_slug = []

    for term in sorted(tags):
        slug = slugs.get(term)
        if not slug:
            missing_slug.append(("tag", term))
            continue
        result = _write_stub_if_missing(pathlib.Path(args.tags_dir), slug, term, args.dry_run)
        counts["tags"][result] += 1

    for term in sorted(brands - excluded_brands):
        slug = slugs.get(term)
        if not slug:
            missing_slug.append(("brand", term))
            continue
        result = _write_stub_if_missing(pathlib.Path(args.brands_dir), slug, term, args.dry_run)
        counts["brands"][result] += 1

    print(f"tags: {counts['tags']}", file=sys.stderr)
    print(f"brands: {counts['brands']}", file=sys.stderr)
    if missing_slug:
        print(f"missing slug entries (skipped, run backfill_term_slugs.py first): {len(missing_slug)}", file=sys.stderr)
        for kind, term in missing_slug[:20]:
            print(f"  [{kind}] {term}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
