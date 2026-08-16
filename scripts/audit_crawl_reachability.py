"""audit_crawl_reachability.py

ビルド済み Hugo 出力 (`hugo/public`) に対して「sitemap + トップからの BFS」を
オフラインで回し、**クローラが実際に到達しうる URL** と、そのうち noindex が
何枚あるかを数える (#5343 のクロールバジェット測定を再現可能にしたもの)。

なぜ audit_site_health.py と別に要るか:
  audit_site_health は本番 (navi.omcha.jp) を HTTP で叩く週次監査で、
  `--delay` を挟んで 1 URL ずつ取るため 13,000 ページ級だと数時間かかり、
  **マージして配信されるまで測れない**。クロールバジェットの施策は
  「pagerSize を変えたら到達 URL が何枚減るか」を**出す前に**知りたいので、
  ビルド成果物を直接読むオフライン版を分けた。HTTP を一切出さないので
  本番負荷ゼロ、フルサイトで数秒〜数十秒で終わる。
  URL 正規化 / sitemap パース / noindex 判定は audit_site_health から import して
  共有する (判定がズレると 2 つのレーンで数字が食い違うため)。

なぜ「被リンク数」ではなく BFS なのか:
  素朴に「内部リンクの本数」を数えると、ページネーションの `page/1/` エイリアスの
  ような**自己参照**を 1 本と数えてしまい、到達性の実態と乖離する。知りたいのは
  「クローラが踏む URL の枚数」なので、リンクグラフを seed から到達可能集合に
  畳んでから数える。

`page/1/` エイリアスの扱い (これを間違えると noindex を過大に数える):
  Hugo はページネーションの 1 ページ目に `page/1/` というエイリアスを出力する。
  中身は meta refresh のリダイレクト HTML で、`<meta name="robots" content="noindex">`
  を**必ず**持つ。これを素直に数えると「noindex ページ」に混ざるが、実体は
  一覧ページ本体へのリダイレクトであって独立したページではない。よって
  `alias` として別カウントし、noindex 集計からは外す。リンクは辿る
  (エイリアス経由でしか到達できない URL があれば到達性としては数えたいため)。

seed:
  - sitemap.xml (sitemapindex なら子 sitemap も) の全 <loc>
  - トップページ (baseURL)
  実運用のクローラはこの 2 つを起点にするので、それに合わせる。

出力:
  - stdout に人間向けサマリ (セクション別の到達数 / noindex 数)
  - --out で JSON。--baseline に前回の JSON を渡すと差分を出す
    (施策前後を同じ物差しで比べるためのもの。#5343 の「3,441 -> 2,840」がこれ)

副作用:
  なし (read-only)。data/ にも書かない。CI からは呼ばない手動レーン。

使い方:
  cd hugo && hugo --destination public   # 先にビルドしておく
  python scripts/audit_crawl_reachability.py --public-dir hugo/public --out before.json
  # ... 変更 ...
  python scripts/audit_crawl_reachability.py --public-dir hugo/public --baseline before.json

Issue: https://github.com/omochairo/amazon/issues/5343
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from collections import Counter, deque
from typing import Any, Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from audit_site_health import (  # noqa: E402
    _meta_robots_noindex,
    normalize_url,
    parse_sitemap_xml,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_crawl_reachability")

DEFAULT_PUBLIC_DIR = "hugo/public"
DEFAULT_SITEMAP = "sitemap.xml"

# URL パスからセクションを決める。前から順に見て最初に当たったものを採る。
# ページネーション (`/tags/x/page/2/`) は section_of が `/page/` の手前を切ってから
# ここに当て、`tags-pagination` の形に組み立てる。
SECTION_RULES = (
    ("/posts/", "posts"),
    ("/products/", "products"),
    ("/tags/", "tags"),
    ("/brands/", "brands"),
    ("/categories/", "categories"),
    ("/author/", "author"),
    ("/price/", "price"),
)


class Page:
    """到達した 1 URL の記録。"""

    __slots__ = ("url", "noindex", "is_alias", "outlinks", "in_sitemap", "exists")

    def __init__(self, url: str) -> None:
        self.url = url
        self.noindex = False
        self.is_alias = False
        self.outlinks: set[str] = set()
        self.in_sitemap = False
        self.exists = False


def detect_base_url(public_dir: pathlib.Path, sitemap_name: str = DEFAULT_SITEMAP) -> str | None:
    """ビルド成果物自身から baseURL を推定する。

    `hugo` と `hugo server` では baseURL が別 (後者は localhost:1313) になり、
    引数で渡し間違えると全リンクがホスト不一致で捨てられて「到達 0」になる。
    成果物側の sitemap の最初の <loc> から取れば取り違えようがない。
    """
    sm = public_dir / sitemap_name
    if not sm.exists():
        return None
    _, locs = parse_sitemap_xml(sm.read_text(encoding="utf-8", errors="replace"))
    if not locs:
        return None
    parsed = urlsplit(locs[0])
    return "{}://{}".format(parsed.scheme, parsed.netloc)


def url_to_path(url: str, public_dir: pathlib.Path) -> pathlib.Path:
    """URL を `public` 配下のファイルパスへ写す。

    Hugo の pretty URL は `/foo/` → `foo/index.html`。拡張子付き
    (`/sitemap.xml` など) はそのまま。
    """
    path = urlsplit(url).path
    rel = path.lstrip("/")
    if rel.endswith("/") or not rel:
        rel = rel + "index.html"
    elif "." not in rel.rsplit("/", 1)[-1]:
        rel = rel + "/index.html"
    return public_dir.joinpath(*rel.split("/"))


def _is_alias(soup: BeautifulSoup) -> bool:
    """Hugo の alias (meta refresh リダイレクト) ページか。

    alias は必ず `<meta http-equiv="refresh">` を持ち、本文を持たない。
    noindex も必ず付くので、これを先に判定しないと noindex を過大に数える
    (module docstring の `page/1/` の項を参照)。
    """
    for meta in soup.find_all("meta"):
        if (meta.get("http-equiv") or "").strip().lower() == "refresh":
            return True
    return False


def read_page(url: str, public_dir: pathlib.Path, base_url: str, host: str) -> Page:
    """1 URL 分の HTML を読んで noindex / alias / 発リンクを取り出す。"""
    page = Page(url)
    fpath = url_to_path(url, public_dir)
    if not fpath.exists() or not fpath.is_file():
        return page
    page.exists = True
    if fpath.suffix.lower() not in (".html", ".htm"):
        return page
    soup = BeautifulSoup(fpath.read_text(encoding="utf-8", errors="replace"), "html.parser")
    page.is_alias = _is_alias(soup)
    page.noindex = _meta_robots_noindex(soup)
    for a in soup.find_all("a", href=True):
        norm = normalize_url(a["href"], url, host)
        if norm and norm != url:  # 自己参照は辿らない (module docstring 参照)
            page.outlinks.add(norm)
    return page


def collect_sitemap_urls(
    public_dir: pathlib.Path, base_url: str, host: str, sitemap_name: str = DEFAULT_SITEMAP
) -> set[str]:
    """sitemap.xml (+ sitemapindex の子) の <loc> を集める。ファイルから直接読む。"""
    urls: set[str] = set()
    queue = deque([sitemap_name])
    seen_files: set[str] = set()
    while queue:
        name = queue.popleft()
        if name in seen_files:
            continue
        seen_files.add(name)
        fpath = public_dir / name.lstrip("/")
        if not fpath.exists():
            logger.warning("sitemap not found: %s", fpath)
            continue
        kind, locs = parse_sitemap_xml(fpath.read_text(encoding="utf-8", errors="replace"))
        if kind == "sitemapindex":
            for loc in locs:
                queue.append(urlsplit(loc).path)
        else:
            for loc in locs:
                norm = normalize_url(loc, base_url, host)
                if norm:
                    urls.add(norm)
    return urls


def crawl(
    public_dir: pathlib.Path, base_url: str, seeds: Iterable[str], max_pages: int
) -> dict[str, Page]:
    """seed から発リンクを辿って到達可能な URL 集合を作る (BFS)。"""
    host = urlsplit(base_url).netloc
    pages: dict[str, Page] = {}
    queue = deque(sorted(seeds))
    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if url in pages:
            continue
        page = read_page(url, public_dir, base_url, host)
        pages[url] = page
        for link in page.outlinks:
            if link not in pages:
                queue.append(link)
    if queue:
        logger.warning("hit --max-pages=%d, %d URL(s) left unvisited", max_pages, len(queue))
    return pages


def section_of(url: str) -> str:
    """URL をセクション名へ分類する。ページネーションは元セクション付きで返す。"""
    path = urlsplit(url).path
    if path in ("/", ""):
        return "home"
    if "/page/" in path:
        # `/page/` の手前を切ると末尾のスラッシュも落ちる (`/posts/page/2/` →
        # `/posts`)。SECTION_RULES の prefix はスラッシュ終わりなので、補ってから
        # 当てないとセクション直下のページャだけが取りこぼされて home 扱いになる。
        # 実測 (2026-08-17) では /posts/page/N/ の 85 本が posts-pagination ではなく
        # home-pagination に計上されており、セクション別に読むと誤読していた。
        head = path.split("/page/", 1)[0] + "/"
        for prefix, name in SECTION_RULES:
            if head.startswith(prefix):
                return name + "-pagination"
        # ルート直下 (`/page/2/`) はどの prefix にも当たらない = ホームのページャ。
        return "home-pagination"
    for prefix, name in SECTION_RULES:
        if path.startswith(prefix):
            return name
    return "other"


def summarize(pages: dict[str, Page], sitemap_urls: set[str]) -> dict[str, Any]:
    """到達集合を集計する。alias は noindex から外して別枠にする。"""
    reachable = {u: p for u, p in pages.items() if p.exists}
    missing = sorted(u for u, p in pages.items() if not p.exists)

    by_section: dict[str, Counter] = {}
    for url, page in reachable.items():
        sec = section_of(url)
        c = by_section.setdefault(sec, Counter())
        c["total"] += 1
        if page.is_alias:
            c["alias"] += 1
        elif page.noindex:
            c["noindex"] += 1
        else:
            c["indexable"] += 1

    totals = Counter()
    for c in by_section.values():
        totals.update(c)

    noindex_urls = sorted(
        u for u, p in reachable.items() if p.noindex and not p.is_alias
    )
    return {
        "reachable": len(reachable),
        "indexable": totals["indexable"],
        "noindex": totals["noindex"],
        "alias": totals["alias"],
        "noindex_ratio": round(totals["noindex"] / len(reachable), 4) if reachable else 0.0,
        "sitemap_urls": len(sitemap_urls),
        # sitemap にあるのに BFS でも取れなかった = ビルド出力に無い (要調査)
        "linked_but_missing": missing[:50],
        "linked_but_missing_count": len(missing),
        "by_section": {
            sec: dict(c) for sec, c in sorted(by_section.items())
        },
        "noindex_urls": noindex_urls,
    }


def render_summary(summary: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    """人間向けサマリ。baseline があれば差分を併記する。"""

    def delta(key: str) -> str:
        if not baseline or key not in baseline:
            return ""
        d = summary[key] - baseline[key]
        return " ({:+d})".format(d) if isinstance(d, int) else " ({:+.4f})".format(d)

    lines = [
        "クロール到達性 (sitemap + トップから BFS)",
        "",
        "  到達 URL   : {}{}".format(summary["reachable"], delta("reachable")),
        "  indexable  : {}{}".format(summary["indexable"], delta("indexable")),
        "  noindex    : {}{} ({:.1%})".format(
            summary["noindex"], delta("noindex"), summary["noindex_ratio"]),
        "  alias      : {}{} (page/1 等のリダイレクト。noindex から除外済み)".format(
            summary["alias"], delta("alias")),
        "  sitemap    : {} URL".format(summary["sitemap_urls"]),
        "",
        "  {:<24} {:>7} {:>10} {:>8} {:>6}".format(
            "section", "total", "indexable", "noindex", "alias"),
    ]
    base_sections = (baseline or {}).get("by_section", {})
    for sec, c in summary["by_section"].items():
        b = base_sections.get(sec, {})
        d = ""
        if baseline:
            d = " ({:+d})".format(c.get("total", 0) - b.get("total", 0))
        lines.append("  {:<24} {:>7} {:>10} {:>8} {:>6}{}".format(
            sec, c.get("total", 0), c.get("indexable", 0),
            c.get("noindex", 0), c.get("alias", 0), d))
    if summary["linked_but_missing_count"]:
        lines += [
            "",
            "  リンクされているがビルド出力に無い URL: {} 件".format(
                summary["linked_but_missing_count"]),
        ]
        for u in summary["linked_but_missing"][:10]:
            lines.append("    - {}".format(u))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="ビルド済み Hugo 出力のクロール到達性を測る (#5343)")
    p.add_argument("--public-dir", default=DEFAULT_PUBLIC_DIR)
    p.add_argument("--base-url", default=None,
                   help="既定: sitemap.xml の最初の <loc> から自動判定")
    p.add_argument("--sitemap", default=DEFAULT_SITEMAP)
    p.add_argument("--max-pages", type=int, default=50000)
    p.add_argument("--out", default=None, help="サマリ JSON の書き出し先")
    p.add_argument("--baseline", default=None,
                   help="前回の --out JSON。差分を併記する")
    p.add_argument("--list-noindex", action="store_true",
                   help="到達 noindex URL を全件 stdout に出す")
    args = p.parse_args(argv)

    public_dir = pathlib.Path(args.public_dir)
    if not public_dir.is_dir():
        logger.error("public dir not found: %s (先に hugo でビルドすること)", public_dir)
        return 1

    base_url = args.base_url or detect_base_url(public_dir, args.sitemap)
    if not base_url:
        logger.error("baseURL を判定できません。--base-url を指定してください")
        return 1
    host = urlsplit(base_url).netloc
    logger.info("base url: %s", base_url)

    sitemap_urls = collect_sitemap_urls(public_dir, base_url, host, args.sitemap)
    logger.info("sitemap urls: %d", len(sitemap_urls))

    seeds = set(sitemap_urls) | {base_url.rstrip("/") + "/"}
    pages = crawl(public_dir, base_url, seeds, args.max_pages)
    for url in sitemap_urls:
        if url in pages:
            pages[url].in_sitemap = True

    summary = summarize(pages, sitemap_urls)
    baseline = None
    if args.baseline:
        bpath = pathlib.Path(args.baseline)
        if bpath.exists():
            baseline = json.loads(bpath.read_text(encoding="utf-8"))
        else:
            logger.warning("baseline not found: %s", bpath)

    print(render_summary(summary, baseline))
    if args.list_noindex:
        print("\n到達 noindex URL:")
        for u in summary["noindex_urls"]:
            print("  " + u)

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
        logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
