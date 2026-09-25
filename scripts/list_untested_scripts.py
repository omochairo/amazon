"""テストの無い本番スクリプトを列挙する (#8272 項目4)。

``scripts/*.py`` のうち、``scripts/tests/`` のどこからもモジュール名で参照されて
おらず、かつ workflow / ``.gitlab-ci.yml`` / 他のスクリプトから呼ばれているものを
行数の多い順に出す。#8271 はこの抽出から見つかった。

参照の判定はモジュール名の単語一致なので粗い (コメントで名前が出るだけでも
「参照あり」になる)。レビュー対象を絞るための道具で、定期実行はしない。

使い方:
    python scripts/list_untested_scripts.py [--all]   # --all は呼び出し元の無いものも出す
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def find_untested(root: pathlib.Path) -> list[dict]:
    scripts_dir = root / "scripts"
    modules = sorted(scripts_dir.glob("*.py"))
    tests = "\n".join(_read(p) for p in sorted((scripts_dir / "tests").glob("*.py")))
    callers = {
        p: _read(p)
        for p in [
            *sorted((root / ".github" / "workflows").glob("*.yml")),
            root / ".gitlab-ci.yml",
            *modules,
            *sorted(scripts_dir.glob("*.sh")),
        ]
        if p.exists()
    }

    rows = []
    for mod in modules:
        name = mod.stem
        pat = re.compile(rf"\b{re.escape(name)}\b")
        if pat.search(tests):
            continue
        used_by = [
            p.relative_to(root).as_posix()
            for p, text in callers.items()
            if p != mod and pat.search(text)
        ]
        rows.append({
            "path": mod.relative_to(root).as_posix(),
            "lines": len(_read(mod).splitlines()),
            "used_by": used_by,
        })
    rows.sort(key=lambda r: (-r["lines"], r["path"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="呼び出し元の無いスクリプトも出す")
    args = ap.parse_args()

    rows = find_untested(REPO_ROOT)
    shown = rows if args.all else [r for r in rows if r["used_by"]]
    for r in shown:
        print(f"{r['lines']:5d}  {r['path']}  <- {', '.join(r['used_by']) or '(none)'}")
    print(f"--- {len(shown)} untested script(s)"
          f"{'' if args.all else ' with callers'} / {len(rows)} total untested",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
