"""#2817 Phase 4: 新規タグ/ブランド用語の data/term_slugs.yaml 自動登録。

.github/workflows/21-ensure-tag-slugs.yml (main への push フック) から呼ばれる。
build_post.py は TermSlugMap.get_or_create() を build 時にも呼ぶが save() しない
(壊れた slug を出さないための安全網に留まる、scripts/term_slug.py 参照)。
この script が実際の永続化を担うことで、複数 PR が並行して同じ新規タグを
使った場合の衝突判定 (連番サフィックス) を単一の真実源に対して直列に行う。

backfill_term_slugs.py (Phase 1, 初回一括生成専用) と違い、記事 JSON 側は
「今回の push で追加/変更された分」だけを対象にする (全記事再スキャンは
コストが高く、main への push 毎に走らせるには不向き)。brand_taxonomy.yaml の
canonical はファイル自体が小さいため毎回全件スキャンする。

使い方:
    git diff --name-only <before> <after> -- 'data/articles/*.json' \
        | python scripts/ensure_tag_slugs.py
    # または
    python scripts/ensure_tag_slugs.py --articles data/articles/xxx.json ...
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backfill_term_slugs import _collect_brand_canonicals
from term_slug import TermSlugMap

_SIDECAR_SUFFIXES = (".enrichment.json", ".seo.json", ".quality.json")


def _collect_tags_from_paths(paths: list) -> set:
    tags: set[str] = set()
    for raw in paths:
        p = pathlib.Path(raw.strip())
        if not raw.strip() or p.name.endswith(_SIDECAR_SUFFIXES) or not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for t in data.get("tags", []) or []:
            t = str(t).rstrip(". ").strip()
            if t:
                tags.add(t)
    return tags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--articles",
        nargs="*",
        default=None,
        help="変更された記事 JSON のパス (省略時は改行区切りで stdin から読む)",
    )
    ap.add_argument("--slugs", default="data/term_slugs.yaml")
    ap.add_argument("--brand-taxonomy", default="data/brand_taxonomy.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.articles is not None:
        paths = args.articles
    else:
        paths = [line for line in sys.stdin.read().splitlines() if line.strip()]

    terms = _collect_tags_from_paths(paths) | _collect_brand_canonicals(args.brand_taxonomy)

    slug_map = TermSlugMap(path=args.slugs, brand_taxonomy_path=args.brand_taxonomy)
    new_terms = sorted(t for t in terms if slug_map.get(t) is None)

    if not new_terms:
        print("no new tag/brand terms; term_slugs.yaml unchanged", file=sys.stderr)
        return 0

    for term in new_terms:
        slug = slug_map.get_or_create(term)
        print(f"{term}\t{slug}", file=sys.stderr)
    print(f"registered {len(new_terms)} new term(s)", file=sys.stderr)

    if args.dry_run:
        print("--dry-run: not writing output file.", file=sys.stderr)
        return 0

    slug_map.save()
    print(f"wrote {args.slugs}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
