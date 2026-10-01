"""prune_standby_noindex.py

#6415 の 2: **待機系 GitLab Pages に送る配信物からだけ** noindex の term ページを落とす。

## なぜ要るか

待機系 GitLab Pages には展開後 1 GiB の上限があり、2026-08-28 にはこれを超えて
本番が 19 時間凍った (#6204 / #6205)。配信元は NAS に移ったが、待機系は残すので
上限は生きた制約のまま。54-pages-size-monitor.yml の実測では 2026-09-28 に
786.7 MiB (76.8%)、直近 3 週の伸びは約 5.6 MiB/日で、11 月上旬に上限へ届く。

noindex の term (薄い tag・noindex ブランド・ブランド名や hub と重複する tag 等、
判定は hugo/layouts/partials/is_noindex_term.html) は配信物の大きな部分を占めるが、
sitemap にも、トップや一覧からの導線にも載っていない。

#6206 で「配信物全体から消す」案を不採用にした理由 (404・リダイレクト設計・
内部リンク除去のコスト) は、**平常時の配信元 (NAS) には全量を置き、待機系だけ
落とす**形なら発生しない。待機系に切り替わっている間だけ、それらが 404 になる。

## 判定

term の **1 ページ目** (`<root>/<taxonomy>/<slug>/index.html`) の meta robots が
noindex なら、その term のディレクトリ (page/N/・index.xml を含む) を丸ごと落とす。

- head.html は「term 側の事情による noindex」と「ページ送り 2 ページ目以降の
  noindex」に同じ `noindex, follow` を出すので、HTML だけでは区別できない。ただし
  ページ送りの noindex は page/2/ 以降にしか出ないので、**1 ページ目だけを見れば
  term 側の事情に限られる**。index される term の page/N/ は 1 ページ目が index
  なので残る
- Hugo の alias (meta refresh のリダイレクトページ) も noindex を持つが、移転元の
  URL を生かすためのものなので落とさない。数 KB しかない
- taxonomy 一覧自身のページ送り (`<root>/tags/page/N/`) は term ではないので見ない

テンプレートの述語を Python に写さず、**レンダリング結果の meta robots を正本に
する**。条件を Hugo 側に足しても、ここを触らずに追従する。

## 使いかた (CI: .gitlab-ci.yml の pages ジョブ)

    python scripts/prune_standby_noindex.py --root public
    python scripts/prune_standby_noindex.py --root public --dry-run
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Sequence

DEFAULT_TAXONOMIES = ("tags", "brands", "categories")

# --minify (tdewolff) は引用符を外せる属性値から外す: `<meta name=robots content="noindex, follow">`
_ROBOTS_RE = re.compile(
    r"""<meta\s+name=["']?robots["']?\s+content=["']?([^"'>]*)""",
    re.IGNORECASE,
)
_REFRESH_RE = re.compile(r"""http-equiv=["']?refresh""", re.IGNORECASE)

# ほぼ全 term が消えるような結果は判定の破綻とみなして止める。想定しているのは
# 非 production ビルドで head.html が全ページに noindex を出したケース。
# 正常時でも割合は高い: 2026-10-01 の main ビルドで 3,924 / 4,549 (86.3%)、
# 落とす量は 128.9 MiB (全量 716 MiB)。薄い tag が増えるほど上がるので余裕を取る。
DEFAULT_MAX_RATIO = 0.98


@dataclass
class PruneResult:
    pruned: List[pathlib.Path] = field(default_factory=list)
    kept: int = 0
    pruned_bytes: int = 0

    @property
    def total_terms(self) -> int:
        return len(self.pruned) + self.kept


def is_noindex_page(html: str) -> bool:
    """meta robots が noindex で、alias のリダイレクトページでもない。"""
    m = _ROBOTS_RE.search(html)
    if not m or "noindex" not in m.group(1).lower():
        return False
    return not _REFRESH_RE.search(html)


def _dir_bytes(path: pathlib.Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def find_noindex_terms(root: pathlib.Path,
                       taxonomies: Sequence[str] = DEFAULT_TAXONOMIES) -> PruneResult:
    result = PruneResult()
    for tax in taxonomies:
        tax_dir = root / tax
        if not tax_dir.is_dir():
            continue
        for term_dir in sorted(tax_dir.iterdir()):
            # `<tax>/page/N/` は taxonomy 一覧のページ送りで term ではない
            if not term_dir.is_dir() or term_dir.name == "page":
                continue
            index = term_dir / "index.html"
            if not index.is_file():
                continue
            if is_noindex_page(index.read_text(encoding="utf-8", errors="replace")):
                result.pruned.append(term_dir)
                result.pruned_bytes += _dir_bytes(term_dir)
            else:
                result.kept += 1
    return result


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path("public"))
    ap.add_argument("--taxonomies", default=",".join(DEFAULT_TAXONOMIES),
                    help="カンマ区切り (default: %(default)s)")
    ap.add_argument("--max-ratio", type=float, default=DEFAULT_MAX_RATIO,
                    help="落とす term の割合がこれを超えたら何もせず失敗する")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not (args.root / "index.html").is_file():
        print(f"[prune-standby] {args.root}/index.html が無い (ビルド成果物を取れていない)",
              file=sys.stderr)
        return 1

    taxonomies = [t.strip() for t in args.taxonomies.split(",") if t.strip()]
    result = find_noindex_terms(args.root, taxonomies)
    if result.total_terms == 0:
        print(f"[prune-standby] {args.root} に term が 1 件も無い。ビルドが壊れている可能性が"
              "あるので何もしない", file=sys.stderr)
        return 1
    ratio = len(result.pruned) / result.total_terms
    print(f"[prune-standby] noindex term {len(result.pruned)} / {result.total_terms} "
          f"({ratio:.1%}), {result.pruned_bytes / 1048576:.1f} MiB")

    if ratio > args.max_ratio:
        print(f"[prune-standby] 落とす割合 {ratio:.1%} が上限 {args.max_ratio:.0%} を超えた。"
              "noindex の判定が壊れている可能性があるので何もしない", file=sys.stderr)
        return 1

    if args.dry_run:
        for d in result.pruned:
            print(d.relative_to(args.root).as_posix())
        return 0

    for d in result.pruned:
        shutil.rmtree(d)
    print(f"PRUNED_STANDBY_BYTES={result.pruned_bytes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
