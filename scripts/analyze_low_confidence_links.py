#!/usr/bin/env python3
"""Issue #806: 低確度アフィリエイトリンク監査ツール (scratch 版の永続化).

記事 JSON (data/articles/*.json) の prices.{rakuten,yahoo} のうち
verified=false な未検証リンクを列挙し、ASIN ごとの JAN known/unknown 内訳を
markdown / json で出力する。

scratch/extract_low_confidence_links.py の置き換え。Phase 2 では本スクリプトの
--json 出力を後続の backfill_jan_codes.py が読み込むことを想定。
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any, Iterable


DEFAULT_ARTICLES_DIR = pathlib.Path("data/articles")
DEFAULT_PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_AMAZON_RAW = pathlib.Path("data/raw/amazon.json")
DEFAULT_MD = pathlib.Path("scratch/low_confidence_links.md")

SITE_LABELS = {"rakuten": "楽天市場", "yahoo": "Yahoo!ショッピング"}


def _load_json(path: pathlib.Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_asin_to_jan(
    amazon_raw_path: pathlib.Path,
    per_asin_dir: pathlib.Path,
    asins: Iterable[str],
) -> dict[str, str | None]:
    """ASIN -> JAN マップ。amazon.json を 1 次、per_asin を 2 次に。"""
    mapping: dict[str, str | None] = {}
    raw = _load_json(amazon_raw_path) or {}
    for item in raw.get("items", []) or []:
        a = item.get("asin")
        j = item.get("jan_code")
        if a and j:
            mapping[a] = j

    for asin in asins:
        if mapping.get(asin):
            continue
        snap_path = per_asin_dir / asin / "amazon.json"
        snap = _load_json(snap_path)
        if not snap:
            continue
        item = snap.get("item") if isinstance(snap.get("item"), dict) else snap
        jan = (item or {}).get("jan_code")
        if jan:
            mapping[asin] = jan
    return mapping


def collect_low_confidence(
    articles_dir: pathlib.Path,
    asin_to_jan: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """記事ディレクトリを走査して未検証リンクを集める。"""
    out: list[dict[str, Any]] = []
    for f in sorted(articles_dir.glob("*.json")):
        if f.name.endswith((".enrichment.json", ".seo.json", ".quality.json")):
            continue
        data = _load_json(f)
        if not isinstance(data, dict):
            continue
        product = data.get("product") or {}
        asin = product.get("asin")
        if not asin:
            continue
        prices = product.get("prices") or {}
        jan = (asin_to_jan or {}).get(asin)
        for site_key, site_label in SITE_LABELS.items():
            site = prices.get(site_key)
            if not isinstance(site, dict):
                continue
            url = site.get("url")
            if not url or site.get("verified"):
                continue
            out.append({
                "slug": data.get("slug"),
                "title": data.get("title"),
                "asin": asin,
                "jan": jan,
                "site": site_key,
                "site_label": site_label,
                "price": site.get("price"),
                "url": url,
                "is_search": bool(site.get("is_search", False)),
            })
    return out


def split_by_jan(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """JAN 有無で 2 群に分け、Phase 1/2 担当 ASIN を一意化して返す。"""
    jan_known_asins: dict[str, str] = {}
    jan_unknown_asins: set[str] = set()
    for r in rows:
        if r.get("jan"):
            jan_known_asins.setdefault(r["asin"], r["jan"])
        else:
            jan_unknown_asins.add(r["asin"])
    # unknown は known 側で既に拾えている分を除外 (記事ごとに JAN 有無バラつき対策)
    jan_unknown_asins -= set(jan_known_asins)
    return {
        "jan_known": [{"asin": a, "jan": j} for a, j in sorted(jan_known_asins.items())],
        "jan_unknown": sorted(jan_unknown_asins),
    }


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| 記事 (Slug) | 商品名 (ASIN) | JANコード | ECサイト | 未検証価格/URL | 検索フォールバックか |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        price = r.get("price")
        url = r["url"]
        url_disp = f"[{price}円]({url})" if price else f"[リンク]({url})"
        is_search = "はい" if r["is_search"] else "いいえ"
        jan = r.get("jan") or "不明"
        lines.append(
            f"| [{r['slug']}](data/articles/{r['slug']}.json) | {r['title']} (`{r['asin']}`) "
            f"| `{jan}` | {r['site_label']} | {url_disp} | {is_search} |"
        )
    lines.append(f"\nTotal: {len(rows)} links found.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--articles-dir", type=pathlib.Path, default=DEFAULT_ARTICLES_DIR)
    p.add_argument("--per-asin-dir", type=pathlib.Path, default=DEFAULT_PER_ASIN_DIR)
    p.add_argument("--amazon-raw", type=pathlib.Path, default=DEFAULT_AMAZON_RAW)
    p.add_argument("--md", type=pathlib.Path, default=None, help="markdown 出力先 (省略時は出力しない)")
    p.add_argument("--json", dest="json_path", type=pathlib.Path, default=None,
                   help='{"jan_known": [...], "jan_unknown": [...]} を JSON 出力')
    p.add_argument("--stats", action="store_true", help="件数だけ stdout に出す")
    args = p.parse_args(argv)

    # 全 article から asin 集合を取り、jan map を構築
    raw_rows_asins: set[str] = set()
    for f in args.articles_dir.glob("*.json"):
        if f.name.endswith((".enrichment.json", ".seo.json", ".quality.json")):
            continue
        data = _load_json(f) or {}
        asin = (data.get("product") or {}).get("asin")
        if asin:
            raw_rows_asins.add(asin)

    asin_to_jan = build_asin_to_jan(args.amazon_raw, args.per_asin_dir, raw_rows_asins)
    rows = collect_low_confidence(args.articles_dir, asin_to_jan)
    split = split_by_jan(rows)

    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_markdown(rows), encoding="utf-8")
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.stats or not (args.md or args.json_path):
        print(f"total_links={len(rows)}")
        print(f"jan_known_asins={len(split['jan_known'])}")
        print(f"jan_unknown_asins={len(split['jan_unknown'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
