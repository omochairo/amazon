#!/usr/bin/env python3
"""Issue #806 Phase 2-2: JAN unknown な ASIN に対し Creator API から externalIds を
再取得して per_asin/<asin>/amazon.json と data/raw/amazon.json に書き戻す.

入力: scripts/analyze_low_confidence_links.py --json の出力
    {"jan_known": [...], "jan_unknown": ["B001", "B002", ...]}

Creator API getItems を 10 件ずつバッチ呼び出し、itemInfo.externalIds.eans
(無ければ isbns) から JAN を取り、見つかった ASIN だけ書き戻す。

dry-run mode 必須: --apply を付けない限りファイル変更しない。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from creators_api_client import CreatorsAPIClient  # noqa: E402


BATCH_SIZE = 10
JAN_RESOURCES = ["itemInfo.externalIds"]
DEFAULT_PER_ASIN = pathlib.Path("data/raw/per_asin")
DEFAULT_AMAZON_RAW = pathlib.Path("data/raw/amazon.json")


def _safe_get(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def extract_jan_from_item(item: dict) -> str:
    """Creator API getItems response 1 件から JAN (EAN preferred, ISBN fallback) を取り出す."""
    for key in ("eans", "isbns", "upcs"):
        values = _safe_get(item, "itemInfo", "externalIds", key, "displayValues", default=[]) or []
        if values and isinstance(values[0], str) and values[0].strip():
            return values[0].strip()
    return ""


def fetch_jans(client: CreatorsAPIClient, asins: list[str]) -> dict[str, str]:
    """ASIN → JAN マップ。見つからなかった ASIN は含まれない."""
    found: dict[str, str] = {}
    for batch_start in range(0, len(asins), BATCH_SIZE):
        batch = asins[batch_start:batch_start + BATCH_SIZE]
        resp = client.get_items(batch, resources=JAN_RESOURCES)
        items = _safe_get(resp, "itemsResult", "items", default=[]) or []
        for item in items:
            asin = item.get("asin") or item.get("itemId")
            jan = extract_jan_from_item(item)
            if asin and jan:
                found[asin] = jan
        time.sleep(0.5)  # gentle pacing between batches
    return found


def update_per_asin(per_asin_dir: pathlib.Path, asin: str, jan: str) -> bool:
    """per_asin/<asin>/amazon.json を更新。実書き込みあれば True."""
    path = per_asin_dir / asin / "amazon.json"
    if not path.exists():
        return False
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    item = snap.get("item") if isinstance(snap.get("item"), dict) else None
    target = item if item is not None else snap
    if target.get("jan_code"):
        return False  # 既に埋まっている (analyze から time gap で別経路埋まり) → skip
    target["jan_code"] = jan
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def update_amazon_raw(amazon_raw: pathlib.Path, asin_to_jan: dict[str, str]) -> int:
    """data/raw/amazon.json items[] に jan_code を埋める. 更新件数を返す."""
    if not amazon_raw.exists():
        return 0
    data = json.loads(amazon_raw.read_text(encoding="utf-8"))
    updated = 0
    for item in data.get("items", []) or []:
        a = item.get("asin")
        if a in asin_to_jan and not item.get("jan_code"):
            item["jan_code"] = asin_to_jan[a]
            updated += 1
    if updated:
        amazon_raw.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=pathlib.Path, required=True,
                   help="analyze_low_confidence_links.py --json の出力")
    p.add_argument("--per-asin-dir", type=pathlib.Path, default=DEFAULT_PER_ASIN)
    p.add_argument("--amazon-raw", type=pathlib.Path, default=DEFAULT_AMAZON_RAW)
    p.add_argument("--apply", action="store_true",
                   help="未指定なら dry-run (Creator API 呼びはするが書き戻ししない)")
    p.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ処理 (0=全件)")
    p.add_argument("--report", type=pathlib.Path, default=pathlib.Path("scratch/jan_backfill_report.json"))
    args = p.parse_args(argv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    asins: list[str] = list(payload.get("jan_unknown") or [])
    if args.limit > 0:
        asins = asins[: args.limit]
    if not asins:
        print("no jan_unknown ASINs to process")
        return 0

    if not os.environ.get("AMAZON_CREATORS_CREDENTIAL_ID"):
        print("ERROR: AMAZON_CREATORS_* env vars not set", file=sys.stderr)
        return 2

    client = CreatorsAPIClient()
    found = fetch_jans(client, asins)
    print(f"queried={len(asins)} jan_found={len(found)}")

    per_asin_updated = 0
    raw_updated = 0
    if args.apply and found:
        for asin, jan in found.items():
            if update_per_asin(args.per_asin_dir, asin, jan):
                per_asin_updated += 1
        raw_updated = update_amazon_raw(args.amazon_raw, found)
        print(f"per_asin_updated={per_asin_updated} amazon_raw_updated={raw_updated}")
    else:
        print("(dry-run — no files written)")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "queried": len(asins),
        "jan_found": found,
        "apply": bool(args.apply),
        "per_asin_updated": per_asin_updated,
        "amazon_raw_updated": raw_updated,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
