"""fix_third_party_host_20260820.py

`fetch_third_party_sources._host` が `lstrip("www.")` を使っていたため、"w" または
"." で始まる host の先頭文字が削られて保存されていた (walmart.com → almart.com /
watch.impress.co.jp → atch.impress.co.jp)。lstrip は接頭辞ではなく**文字集合**を
削るのが原因。関数側は修正済みだが、既に保存された JSON は直らないのでここで直す。

影響 (2026-08-20 実測):
  - data/raw/per_asin/**/third_party_sources.json の source 行 84 件 / 81 ASIN
  - うち 10 件は配信物の source_highlights に**読者向けの出典表示**として出ていた
    (例: 2026-07-11-B0002AHQWS.md の「出典：almart.com」)
  - build_post._HIGHLIGHT_HOST_DENY の "wish.com" は host 側が "ish.com" に
    なるため一致せず、deny が効かない状態だった

freshness skip (既定 30 日 / 既存記事レーンは 90 日) があるうえ、収集済みの ASIN は
母集合から外れるため、放置すると再取得で自然に直ることは期待できない。

やること: `host` フィールドが URL から導いた正しい値と食い違う行だけを書き換える。
URL・タイトル・snippet は触らない。ネットワークアクセスなし。

Usage:
    python scripts/_oneoff/fix_third_party_host_20260820.py --dry-run
    python scripts/_oneoff/fix_third_party_host_20260820.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from fetch_third_party_sources import OUT_NAME, _host  # noqa: E402

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
REPORT = pathlib.Path(__file__).with_suffix(".report.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="third_party_sources.json の host 修正")
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    base = pathlib.Path(args.base)

    fixed: list[dict] = []
    files = 0
    for d in sorted(base.iterdir()):
        path = d / OUT_NAME
        if not path.is_dir() and not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list):
            continue
        dirty = False
        for s in sources:
            if not isinstance(s, dict):
                continue
            url = s.get("url") or ""
            stored = s.get("host") or ""
            correct = _host(url)
            # URL から host が取れない行 (空・不正) は触らない。
            if not correct or stored == correct:
                continue
            fixed.append({"asin": d.name, "url": url, "from": stored, "to": correct})
            s["host"] = correct
            dirty = True
        if dirty:
            files += 1
            if not args.dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"{'[dry-run] ' if args.dry_run else ''}修正 {len(fixed)} 行 / {files} ファイル")
    for r in fixed[:20]:
        print(f"  {r['asin']}  {r['from']} -> {r['to']}")
    if len(fixed) > 20:
        print(f"  ... 他 {len(fixed) - 20} 行")
    if not args.dry_run:
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump({"rows": len(fixed), "files": files, "fixed": fixed},
                      f, ensure_ascii=False, indent=2)
        print(f"レポート: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
