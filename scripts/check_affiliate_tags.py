"""check_affiliate_tags.py

配信物 (Hugo の出力 HTML) に含まれる Amazon リンクが全て
``tag=<hugo/config.toml の [params].amazonPartnerTag>`` を持つことを検証する。

omcha-ops#19 P1 の catch 層。prevent 層は 3 つ:
  - scripts/build_post.py の ``_force_amazon_partner_tag`` (記事 Markdown)
  - hugo/layouts/partials/amazon_affiliate_url.html (一覧系カードの CTA)
  - scripts/fetch_amazon.py (タグ不在なら書き込まずに落ちる)

**quality_gate.py ではこれを検出できない。** あちらの ``_normalize_url`` は
URL 重複判定のために ``tag=`` を意図的に剥がしており、剥がした後で比較する
設計になっている。タグの有無はレンダリング結果でしか判定できないので、
記事 JSON を見るゲートではなく配信物を見るこのスクリプトが担当する。

使い方 (CI: .gitlab-ci.yml の pages ジョブ、hugo build の直後):

    python scripts/check_affiliate_tags.py --root public

終了コード: 違反ゼロなら 0、1 本でもあれば 1。
"""
from __future__ import annotations

import argparse
import html
import pathlib
import re
import sys
import tomllib

# href/src 属性値の中の amazon.co.jp リンクだけを見る。本文にテキストとして
# 現れた URL はクリックされないので対象外 (誤検出を増やすだけ)。
#
# 配信ビルドは `hugo --minify`。minify は引用符の要らない属性値から引用符を外す
# (実測: `href="https://www.amazon.co.jp/dp/B0ABC/"` → `href=https://www.amazon.co.jp/dp/B0ABC/`)。
# `=` を含む URL (`?tag=` 付き) は引用符が残るので、タグ無しの違反リンクほど
# 引用符なしで出てくる。引用符なし形式も見ないと、違反だけがすり抜ける (#11004)。
_HREF_AMAZON_RE = re.compile(
    r"""href=(?:
        "(?P<dq>https?://(?:www\.)?amazon\.co\.jp/[^"]*)"
      | '(?P<sq>https?://(?:www\.)?amazon\.co\.jp/[^']*)'
      | (?P<uq>https?://(?:www\.)?amazon\.co\.jp/[^\s"'<>`]*)
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_TAG_RE = re.compile(r"[?&]tag=(?P<tag>[^&#\"']*)")

# fragment より後ろの `?tag=` は Amazon に届かない。post.md.j2 の affiliate_url
# マクロが `.../dp/ASIN/#customerReviews` に素朴に `?tag=` を継いで
# `#customerReviews?tag=chk01-22` を作っていた実例がある (見た目は tag 付きだが
# 収益ゼロ)。判定は必ず fragment を落としてから行う。

# 画像 CDN (images-na.ssl-images-amazon.com 等) はアフィリエイトリンクではない。
# 上の正規表現は amazon.co.jp ホストのみに一致するので追加の除外は不要。


def load_expected_tag(hugo_config: pathlib.Path) -> str:
    with hugo_config.open("rb") as f:
        config = tomllib.load(f)
    params = config.get("params") or {}
    tag = params.get("amazonPartnerTag")
    if not tag or not isinstance(tag, str):
        raise SystemExit(
            f"[params].amazonPartnerTag not set in {hugo_config} (#5087 SSOT)"
        )
    return tag


def scan(root: pathlib.Path, expected: str) -> tuple[int, list[tuple[str, str, str]]]:
    """(検査した Amazon リンク総数, 違反 [(file, url, reason)]) を返す。"""
    total = 0
    violations: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in _HREF_AMAZON_RE.finditer(text):
            # 属性値の & は HTML では &amp; と書く (正しい書き方)。ブラウザが送る URL と
            # 同じものを見るため、戻してから判定する。戻さないと `?rh=…&amp;tag=` を
            # 「tag= が無い」と誤判定し、配信ビルドが止まる (2026-10-29 #9449 の帯で発生)
            url = html.unescape(m.group("dq") or m.group("sq") or m.group("uq"))
            total += 1
            tag_m = _TAG_RE.search(url.partition("#")[0])
            rel = str(path.relative_to(root))
            if tag_m is None:
                violations.append((rel, url, "no tag= (before the fragment)"))
            elif tag_m.group("tag") != expected:
                violations.append((rel, url, f"tag={tag_m.group('tag')!r} != {expected!r}"))
    return total, violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="public", help="Hugo の出力ディレクトリ")
    ap.add_argument("--hugo-config", default="hugo/config.toml")
    ap.add_argument("--max-report", type=int, default=30,
                    help="出力する違反の件数上限 (全件数は必ず表示する)")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"[check_affiliate_tags] root not found: {root}", file=sys.stderr)
        return 1

    expected = load_expected_tag(pathlib.Path(args.hugo_config))
    total, violations = scan(root, expected)

    if total == 0:
        # 0 本で OK にすると、href の書式が変わって regex が全件外したときも緑で通る。
        # サイトには必ず Amazon リンクがあるので、0 本は検査が空振りした合図 (#11004)
        print(
            f"[check_affiliate_tags] FAIL: no Amazon links found under {root} "
            "(the check scanned nothing)",
            file=sys.stderr,
        )
        return 1

    if not violations:
        print(f"[check_affiliate_tags] OK: {total} Amazon links, all tag={expected}")
        return 0

    print(
        f"[check_affiliate_tags] FAIL: {len(violations)} / {total} Amazon links "
        f"do not carry tag={expected}",
        file=sys.stderr,
    )
    for rel, url, reason in violations[: args.max_report]:
        print(f"  {rel}: {url}  ({reason})", file=sys.stderr)
    if len(violations) > args.max_report:
        print(f"  ... and {len(violations) - args.max_report} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
