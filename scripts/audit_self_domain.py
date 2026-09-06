#!/usr/bin/env python3
"""収集済みデータに自社 URL が混ざっていないか点検する (#6593)。

## なぜ必要か

外部の Web から集めた素材に自社記事が混ざると、自分の書いたものを自分の根拠に
する循環になる。厄介なのは **混ざっても収集は成功する**こと — レーンは緑のまま
静かに汚染される。#6588 でこれが実在すると分かった (agy の Web 検索が
navi.omcha.jp の当該 ASIN 記事そのものを「購入者の口コミ」の出典として返した)。

収集側のガードは各レーンに入れた (self_domain.is_self_domain) が、**ガードは
壊れても気づけない**。ここは結果側から見る catch 側の計器。

## 何を見るか

per_asin の外部ソース系ファイルと、体験談の出典 URL を走査して、自社ドメインの
URL が何件あるかを出す。**0 件であることが正常。**

意図的な自己参照は対象外にしてある:
  - omcha_related.json … 内部リンク用。設計どおり自社 URL しか入らない
  - data/analytics/**  … GSC/GA4/Lighthouse 等、自社サイトの計測データ

使い方:
  python3 -m scripts.audit_self_domain
  python3 -m scripts.audit_self_domain --json      # CI から読む用
  python3 -m scripts.audit_self_domain --strict    # 1 件でもあれば非ゼロ終了
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    from scripts import self_domain
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts import self_domain

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")

# 走査対象。**意図的に自社を含むファイルは入れないこと** (omcha_related.json 等)。
TARGET_FILES = (
    "third_party_sources.json",  # Google 検索由来の第三者ソース
    "news.json",                 # Google News RSS 検索由来
    "youtube.json",              # YouTube Data API 由来
    "competitors.json",          # Amazon API 由来
    "experience.json",           # 体験談 (source_url / source_urls)
)


def iter_urls(node) -> list[str]:
    """JSON を再帰的に辿って URL らしき文字列を集める。

    キー名を決め打ちにしないのは、レーンごとに `url` / `link` / `source_url` /
    `source_urls` とばらばらで、**新しいキーが増えたときに黙って見落とす**方が
    怖いから。
    """
    out: list[str] = []
    if isinstance(node, str):
        if node.startswith("http://") or node.startswith("https://"):
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out += iter_urls(v)
    elif isinstance(node, list):
        for v in node:
            out += iter_urls(v)
    return out


def audit(base: pathlib.Path = PER_ASIN_DIR, targets: tuple[str, ...] = TARGET_FILES) -> dict:
    per_file: dict[str, dict] = {name: {"scanned": 0, "urls": 0, "hits": []} for name in targets}
    if not base.exists():
        return {"per_file": per_file, "total_hits": 0, "base_missing": True}

    for asin_dir in sorted(base.iterdir()):
        if not asin_dir.is_dir():
            continue
        for name in targets:
            path = asin_dir / name
            if not path.exists():
                continue
            stat = per_file[name]
            stat["scanned"] += 1
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for url in iter_urls(data):
                stat["urls"] += 1
                if self_domain.is_self_domain(url):
                    stat["hits"].append({"asin": asin_dir.name, "url": url})

    total = sum(len(v["hits"]) for v in per_file.values())
    return {"per_file": per_file, "total_hits": total, "base_missing": False}


def format_report(result: dict) -> str:
    lines = [f"{'file':26} {'走査':>7} {'URL':>8} {'自社 URL':>9}", "-" * 54]
    for name, stat in result["per_file"].items():
        lines.append(f"{name:26} {stat['scanned']:>7} {stat['urls']:>8} {len(stat['hits']):>9}")
    lines.append("")
    if result["total_hits"] == 0:
        lines.append("自社 URL の混入は 0 件。")
    else:
        lines.append(f"**自社 URL が {result['total_hits']} 件**:")
        for name, stat in result["per_file"].items():
            for hit in stat["hits"][:20]:
                lines.append(f"  {name} {hit['asin']} {hit['url']}")
    # 走査 0 件は「きれい」ではなく「測れていない」。区別できないと嘘をつく
    empty = [n for n, s in result["per_file"].items() if s["scanned"] == 0]
    if empty:
        lines.append("")
        lines.append(f"注意: 走査対象が 0 件だったファイル: {', '.join(empty)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=pathlib.Path, default=PER_ASIN_DIR)
    ap.add_argument("--json", action="store_true", help="JSON で出す")
    ap.add_argument("--strict", action="store_true", help="1 件でもあれば非ゼロ終了")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    result = audit(args.base)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(format_report(result))
    return 1 if (args.strict and result["total_hits"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
