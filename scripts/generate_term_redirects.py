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
で本物の HTTP 301 をサポートする。

実機検証 (#2817 Phase 5) で確定した仕様:
- GitLab Pages の `_redirects` パーサ (tj/go-redirects) は各行を
  strings.Fields() (空白区切り) でトークナイズする。1行でも3列に収まらない
  行があるとパースエラーとなり **ファイル全体のリダイレクトが丸ごと
  無効化される** (最初のデプロイでこれを踏んだ実障害あり)。
- マッチングは受信リクエストパスを decode した生の文字列と、ルール from列の
  生の文字列 (percent-encode しない) を比較して行われる。percent-encoded
  な from 列は実際には一致しない (無意味な行だったことを実機で確認済み)。
- 上記2点の帰結として、term に生の空白を含む場合 ((a) raw 行だと3列に
  収まらずファイル全体を壊す、(b) percent-encoded 行だと一致しない) は
  `_redirects` では安全に表現する方法が無いため、その用語は redirect 生成を
  スキップする (旧 URL は 404 のまま = 現状維持。対象は少数
  ・#2817 コメント参照)。

配信ホストを NAS の nginx へ移す (omochairo/amazon#6205) にあたり、同じルールを
nginx の `map` 形式でも出せるようにした。**両方を出し続ける**のは、GitLab Pages を
日次更新の待機系として残すため (`_redirects` はそちら専用)。

nginx map 版が `_redirects` 版と違う点:
- **キーをクォートできるので、生の空白を含む用語も表現できる。** `_redirects` では
  3列に収まらずファイル全体を壊すためスキップしていた用語 (#2817 Phase 5) を拾える。
- nginx の `$uri` はデコード済みなので、生の JP 用語がそのまま一致する
  (GitLab Pages の tj/go-redirects と同じ挙動)。

**map ファイルは 1 行でも壊れていると nginx が起動しない**
(`[emerg] invalid number of the map parameters`)。出力側で制御文字を弾き、
`"` とバックスラッシュをエスケープしているのはこのため。

使い方:
    python scripts/generate_term_redirects.py [--sitemap hugo/public/sitemap.xml]
    python scripts/generate_term_redirects.py --format both         --out public/_redirects --out-map public/redirects.map
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

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
            if any(ch.isspace() for ch in term):
                # _redirects では表現不可能 (モジュール docstring 参照)。
                # 旧 URL は 404 のまま (= 現状維持) にとどめる。
                continue
            new_path = f"/{kind}/{slug}/"
            old_path = f"/{kind}/{term}/"
            lines.append(f"{old_path} {new_path} 301")
    return lines


# nginx の設定値として安全に置けない文字。制御文字が混ざると map ファイルが壊れ、
# **オリジンが丸ごと起動しなくなる**ので、その用語だけ落とす。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _nginx_quote(value: str) -> str:
    """nginx のクォート文字列として安全な形にする。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_map_lines(indexed: dict, reverse: dict) -> list:
    """nginx の `map` に include できる行を作る。

    `_redirects` 版 (build_redirect_lines) との差は 3 点:
    - キーをクォートするので、生の空白を含む用語もスキップせずに拾える
    - 行末に `;` が要る
    - **nginx の map はキーの大文字小文字を区別しない。** 大文字違いで別スラッグに
      なっている用語 (例: `Connetix` -> connetix / `CONNETIX` -> connetix-2) は
      キーが衝突し、nginx が `[emerg] conflicting parameter` で**起動しなくなる**
      (2026-08-30 に実 corpus で実測)。宛先が割れる衝突は**群ごと落とす**。
      どちらに飛ばすかを勝手に決めるより、旧 URL を 404 のまま残す方が安全。
    """
    pairs = []
    for kind in ("tags", "brands"):
        for slug in sorted(indexed[kind]):
            term = reverse.get(slug)
            if not term or term == slug:
                continue
            if _CONTROL_RE.search(term):
                # 制御文字は map ファイル自体を壊しうる。この用語だけ落とす。
                continue
            pairs.append((f"/{kind}/{term}/", f"/{kind}/{slug}/"))

    by_key = {}
    for old_path, new_path in pairs:
        by_key.setdefault(old_path.lower(), []).append((old_path, new_path))

    lines = []
    dropped = 0
    for group in by_key.values():
        targets = {new for _, new in group}
        if len(targets) > 1:
            dropped += len(group)
            print(
                "skip (conflicting parameter): "
                + ", ".join(f"{old} -> {new}" for old, new in group),
                file=sys.stderr,
            )
            continue
        old_path, new_path = group[0]
        lines.append(f'"{_nginx_quote(old_path)}" "{new_path}";')
    if dropped:
        print(f"dropped {dropped} map rules due to case-insensitive key conflicts", file=sys.stderr)
    return sorted(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemap", default="hugo/public/sitemap.xml")
    ap.add_argument("--slugs", default="data/term_slugs.yaml")
    ap.add_argument("--out", default="hugo/static/_redirects")
    ap.add_argument("--out-map", default="hugo/public/redirects.map")
    ap.add_argument(
        "--format",
        choices=("netlify", "nginx", "both"),
        default="netlify",
        help="netlify=_redirects (GitLab Pages) / nginx=map (NAS オリジン) / both",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sitemap_path = pathlib.Path(args.sitemap)
    if not sitemap_path.exists():
        print(f"sitemap not found: {sitemap_path} (run `hugo build` first)", file=sys.stderr)
        return 1

    indexed = _indexed_slugs_by_kind(sitemap_path)
    reverse = _reverse_slug_map(pathlib.Path(args.slugs))

    want_netlify = args.format in ("netlify", "both")
    want_nginx = args.format in ("nginx", "both")

    lines = build_redirect_lines(indexed, reverse) if want_netlify else []
    map_lines = build_map_lines(indexed, reverse) if want_nginx else []

    print(
        f"indexed tags={len(indexed['tags'])} brands={len(indexed['brands'])} "
        f"-> _redirects rules={len(lines)} map rules={len(map_lines)}",
        file=sys.stderr,
    )

    if args.dry_run:
        for line in (lines or map_lines)[:20]:
            print(line, file=sys.stderr)
        print("--dry-run: not writing output file.", file=sys.stderr)
        return 0

    header = (
        "# #2817 Phase 3: 旧 JP タグ/ブランド URL → 新英語スラッグ URL への 301 リダイレクト。\n"
        "# scripts/generate_term_redirects.py で自動生成 (直接編集しない)。\n"
    )

    if want_netlify:
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {out_path} ({len(lines)} rules)", file=sys.stderr)

    if want_nginx:
        # nginx は起動時にこのファイルを include する。**1 行でも壊れていると
        # オリジンが起動しない**ので、生成側で制御文字を弾き、キーをクォートする。
        map_path = pathlib.Path(args.out_map)
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(header + "\n".join(map_lines) + "\n", encoding="utf-8")
        print(f"wrote {map_path} ({len(map_lines)} rules)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
