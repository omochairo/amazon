"""#2817 Phase 3: 旧 JP タグ/ブランド URL → 新英語スラッグ URL への 301 リダイレクト
(hugo/static/_redirects) を生成する。

対象は移行時点で index,follow だった (= 検索エンジンに評価されていた) ものだけ。
noindex だった旧 URL はリダイレクト不要 (ユーザー指示: 2026-07-08)。

「index,follow だったか」の判定は、head.html / sitemap.xml の noindex 条件
(薄タグ閾値・is_brand_tag・hub_for_tag・is_low_value_tag・noindex ブランド) を
Python で再実装する代わりに、**既にビルド済みの hugo/public/sitemap.xml を
そのまま真実源として使う**。理由: これらの除外条件はいずれも JP 用語文字列や
記事数をキーにしており URL スラッグ自体には依存しないため、移行後の sitemap に
その slug が載っていれば「移行前も同じ JP 用語で index 対象だった」と判定して
問題ない (#2817 Phase 2 で .Title は JP 表示のまま保たれることを検証済み)。

ホスティングは GitLab Pages (Cloudflare ではない)。`_redirects` は Netlify 形式
で本物の HTTP 301 をサポートする。非 ASCII (JP) パスのマッチングに使う内部
エンコーディングの明確な一次情報が得られなかったため、**生 UTF-8 と
percent-encoded の両方の形式を安全側で両方出力する** (二重化のコストは小さく、
どちらの形式でマッチしても redirect が成立する)。

使い方:
    python scripts/generate_term_redirects.py [--sitemap hugo/public/sitemap.xml]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import urllib.parse

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_LOC_RE = re.compile(r"<loc>(.*?)</loc>")
_TERM_PATH_RE = re.compile(r"^https?://[^/]+/(tags|brands)/([^/]+)/$")


def _indexed_slugs_by_kind(sitemap_path: pathlib.Path) -> dict:
    """sitemap.xml から {"tags": {slug, ...}, "brands": {slug, ...}} を抽出する。
    ベースの /tags/ /brands/ 一覧ページ自体 (slug 空文字) は対象外。
    """
    content = sitemap_path.read_text(encoding="utf-8")
    out = {"tags": set(), "brands": set()}
    for loc in _LOC_RE.findall(content):
        m = _TERM_PATH_RE.match(loc)
        if not m:
            continue
        kind, slug = m.group(1), m.group(2)
        out[kind].add(slug)
    return out


def _reverse_slug_map(slugs_path: pathlib.Path) -> dict:
    """slug -> JP/元用語 の逆引き (スラッグは一意なので 1:1)。"""
    data = yaml.safe_load(slugs_path.read_text(encoding="utf-8")) or {}
    return {slug: term for term, slug in data.items()}


def build_redirect_lines(indexed: dict, reverse: dict) -> list:
    lines = []
    for kind in ("tags", "brands"):
        for slug in sorted(indexed[kind]):
            term = reverse.get(slug)
            if not term or term == slug:
                continue
            new_path = f"/{kind}/{slug}/"
            raw_old = f"/{kind}/{term}/"
            lines.append(f"{raw_old} {new_path} 301")
            encoded_term = urllib.parse.quote(term, safe="")
            encoded_old = f"/{kind}/{encoded_term}/"
            if encoded_old != raw_old:
                lines.append(f"{encoded_old} {new_path} 301")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemap", default="hugo/public/sitemap.xml")
    ap.add_argument("--slugs", default="data/term_slugs.yaml")
    ap.add_argument("--out", default="hugo/static/_redirects")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sitemap_path = pathlib.Path(args.sitemap)
    if not sitemap_path.exists():
        print(f"sitemap not found: {sitemap_path} (run `hugo build` first)", file=sys.stderr)
        return 1

    indexed = _indexed_slugs_by_kind(sitemap_path)
    reverse = _reverse_slug_map(pathlib.Path(args.slugs))
    lines = build_redirect_lines(indexed, reverse)

    print(
        f"indexed tags={len(indexed['tags'])} brands={len(indexed['brands'])} "
        f"-> redirect rules={len(lines)}",
        file=sys.stderr,
    )

    if args.dry_run:
        for line in lines[:20]:
            print(line, file=sys.stderr)
        print("--dry-run: not writing output file.", file=sys.stderr)
        return 0

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# #2817 Phase 3: 旧 JP タグ/ブランド URL → 新英語スラッグ URL への 301 リダイレクト。\n"
        "# scripts/generate_term_redirects.py で自動生成 (直接編集しない)。\n"
    )
    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(lines)} rules)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
