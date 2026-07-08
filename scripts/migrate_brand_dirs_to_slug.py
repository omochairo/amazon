"""#2817 Phase 2 一回限りの移行: hugo/content/brands/<JP or 表記ゆれ>/ を
data/term_slugs.yaml のスラッグ名へ git mv でリネームする。

対象は 18 件全て (JP名だけでなく "Apitor"→"apitor" 等の大文字小文字/スペース違いも
含む — 全件が現行スラッグと不一致だったため)。title front matter は既存のまま
(全件で JP/元表記の title が既に明示されているため変更不要)。

大文字小文字のみの違い (例: Connetix→connetix) は大文字小文字を区別しない
ファイルシステム (Windows/macOS既定) では git mv が一発で通らないことがあるため、
一時名を経由する2段階リネームを行う。

使い方:
    python scripts/migrate_brand_dirs_to_slug.py [--dry-run]
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BRANDS_DIR = REPO_ROOT / "hugo" / "content" / "brands"
SLUGS_PATH = REPO_ROOT / "data" / "term_slugs.yaml"


def _git_mv(old: pathlib.Path, new: pathlib.Path, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] git mv {old} {new}")
        return
    if old.name.lower() == new.name.lower() and old.name != new.name:
        # 大文字小文字のみの違い: 一時名を経由する (Windows/macOS の大文字小文字を
        # 区別しないファイルシステムでの直接リネーム失敗を回避)。
        tmp = old.with_name(old.name + "__tmp_case_rename")
        subprocess.run(["git", "mv", str(old), str(tmp)], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "mv", str(tmp), str(new)], cwd=REPO_ROOT, check=True)
    else:
        subprocess.run(["git", "mv", str(old), str(new)], cwd=REPO_ROOT, check=True)
    print(f"renamed: {old.name} -> {new.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slugs = yaml.safe_load(SLUGS_PATH.read_text(encoding="utf-8")) or {}

    renamed, skipped, missing_slug = 0, 0, []
    for entry in sorted(BRANDS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        slug = slugs.get(name)
        if not slug:
            missing_slug.append(name)
            continue
        if slug == name:
            skipped += 1
            continue
        _git_mv(entry, entry.with_name(slug), args.dry_run)
        renamed += 1

    print(f"renamed={renamed} skipped(already-slug)={skipped} missing_slug={len(missing_slug)}", file=sys.stderr)
    if missing_slug:
        print(f"  no slug entry for: {missing_slug}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
