"""deploy-nas: 配信物の mtime を「中身のハッシュから決まる値」にそろえる。

rsync の `--checksum` をやめるための下ごしらえ (amazon-home-ops#251)。

`--checksum` は送り側だけでなく**受け側 (NAS) でも全ファイルを読んでハッシュを取る**。
受け側は --link-dest=/current の全ファイル (配信物の全量) を毎デプロイ HDD から
読み直すことになり、NAS の I/O を詰まらせていた。

hugo は毎ビルドで全ファイルを書き直すので、mtime はそのままでは当てにならない
(これが --checksum を付けていた理由)。そこで送る前に、各ファイルの mtime を
中身の sha256 から決まる値に書き換える。同じ中身なら毎回同じ mtime になるので、
rsync -t の既定の比較 (大きさ + mtime) で「変わったファイルだけ」が正しく判定でき、
--link-dest のハードリンク共有も効く。受け側は stat するだけで中身を読まない。

中身が変わったのに大きさも mtime も一致して取りこぼすのは、大きさが同じうえで
ハッシュ由来の値 (2^30 通り) が偶然一致したときだけ。

    python3 scripts/ci_stable_mtimes.py public
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# 2001-09-09 から約 34 年の範囲に収める (未来の時刻や 0 付近を避ける)
_BASE = 1_000_000_000
_SPAN = 1 << 30


def stable_mtime(path: Path) -> int:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return _BASE + int.from_bytes(h.digest()[:8], "big") % _SPAN


def apply(root: Path) -> int:
    n = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            t = stable_mtime(p)
            os.utime(p, (t, t))
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    args = ap.parse_args(argv)
    if not args.root.is_dir():
        print(f"ci_stable_mtimes: not a directory: {args.root}", file=sys.stderr)
        return 2
    n = apply(args.root)
    print(f"ci_stable_mtimes: {n} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
