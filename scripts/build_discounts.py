"""build_discounts.py

#3332 価格OFFバッジのサイト全面展開 — /deals/ 限定だった「定価からN%OFF」バッジを
トップページ各ブロック・ハブ・記事一覧・カルーセルの商品カードにも表示するための
ビルド時集約スクリプト。

``data/raw/per_asin/*/amazon.json`` (fetch_amazon.py が書く生データ。**週次**
fetch レーン由来。``{"asin": str, "fetched_at": ISO, "item":
{"savings_percentage": int, "price": int, ...}}`` 形式) に加えて、
``data/price_watch/latest.json`` (fetch_price_watch.py が書く生データ。**日次**
NAS price-watch レーン由来。#3046/#2995 案8) の ``off`` フィールドを走査し、
閾値を満たす ASIN のみを ``hugo/data/discounts.json`` に書き出す。
front matter に依存せず、product_card.html 等の共有 partial (discount_badge.html)
が site.Data.discounts から ASIN 単位で lookup する。

データソースの優先順位 (#3332 follow-up 日次フレッシュ化):
  1. **price_watch (優先)**: ``data/price_watch/latest.json`` の各 ASIN の
     ``off`` (>0 のときのみ latest.json に存在する) が ``_MIN_PCT`` 以上、かつ
     ``ts`` が ``_WATCH_STALE_DAYS`` 日以内なら採択 (``src: "price_watch"``)。
  2. **per_asin (フォールバック)**: price_watch で採択されなかった ASIN のみ、
     従来通り per_asin の ``savings_percentage`` (``_MIN_PCT`` 以上・
     ``fetched_at`` が ``_STALE_DAYS`` 日以内) で補完する
     (``src: "per_asin"``)。

price_watch は日次で全公開 ASIN を薄く観測するレーンなので鮮度ガードを短く
(3日) 取れる一方、per_asin は記事巡回のついでの週次観測なので従来通り
21日の緩いガードを維持する (較正可能・observation を見て後日調整)。

``hugo/data/discounts.json`` は build_brand_hub_stats.py / build_price_dashboard.py
と同じ流儀で /hugo gitignore 配下のビルド時派生物であり commit しない。

CLI:
    python scripts/build_discounts.py
    python scripts/build_discounts.py --per-asin-root X --latest Y --out Z
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("build_discounts")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_PER_ASIN_ROOT = _REPO_ROOT / "data" / "raw" / "per_asin"
_DEFAULT_LATEST = _REPO_ROOT / "data" / "price_watch" / "latest.json"
_DEFAULT_OUT = _REPO_ROOT / "hugo" / "data" / "discounts.json"

# ------------------------------------------------------------------------
# 較正可能なしきい値 (observation を見て後日調整する想定。
# build_price_dashboard.py の _DROP_PCT_THRESHOLD 等と同じ流儀)
# ------------------------------------------------------------------------
_MIN_PCT = 10            # この%未満の割引は採択しない (端数ノイズ・誤差除け)
_STALE_DAYS = 21         # per_asin: fetched_at がこの日数より古いスナップショットは採択しない
_WATCH_STALE_DAYS = 3    # price_watch: 日次レーンなので短い鮮度ガード (較正可能)


def _parse_iso8601(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"skip unreadable/invalid json {path}: {e}")
        return None
    return d if isinstance(d, dict) else None


def _load_price_watch_items(
    latest_path: pathlib.Path | str,
    *,
    min_pct: int,
    watch_stale_days: int,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """``data/price_watch/latest.json`` を走査し ASIN -> {pct, price, fetched_at, src} を返す。

    latest.json が存在しない/壊れている場合は空 dict を返す (per_asin
    フォールバックのみで graceful に動作させるため)。
    """
    d = _load_json(str(latest_path))
    if d is None:
        return {}

    raw_items = d.get("items")
    if not isinstance(raw_items, dict):
        return {}

    cutoff = now - timedelta(days=watch_stale_days)
    items: dict[str, dict[str, Any]] = {}

    for asin, entry in raw_items.items():
        if not isinstance(asin, str) or not asin.strip():
            continue
        if not isinstance(entry, dict):
            continue

        off = entry.get("off")
        if not isinstance(off, int) or isinstance(off, bool) or off < min_pct:
            continue

        ts_raw = entry.get("ts")
        ts = _parse_iso8601(ts_raw)
        if ts is None or ts < cutoff:
            continue

        price = entry.get("p")
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            price = None

        items[asin.strip().upper()] = {
            "pct": off,
            "price": price,
            "fetched_at": ts_raw,
            "src": "price_watch",
        }

    return items


def _aggregate_per_asin(
    per_asin_root: pathlib.Path | str,
    *,
    min_pct: int,
    stale_days: int,
    now: datetime,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """``data/raw/per_asin/*/amazon.json`` を走査し ASIN -> {pct, price, fetched_at, src} を返す。

    ``exclude`` に含まれる ASIN (= price_watch で既に採択済み) はスキップする。
    """
    cutoff = now - timedelta(days=stale_days)

    items: dict[str, dict[str, Any]] = {}
    files = sorted(glob.glob(str(pathlib.Path(per_asin_root) / "*" / "amazon.json")))

    for f in files:
        d = _load_json(f)
        if d is None:
            continue

        asin = d.get("asin")
        if not isinstance(asin, str) or not asin.strip():
            continue
        asin = asin.strip().upper()
        if asin in exclude:
            continue

        fetched_at_raw = d.get("fetched_at")
        fetched_at = _parse_iso8601(fetched_at_raw)
        if fetched_at is None or fetched_at < cutoff:
            continue

        item = d.get("item")
        if not isinstance(item, dict):
            continue

        pct = item.get("savings_percentage")
        if not isinstance(pct, int) or isinstance(pct, bool) or pct < min_pct:
            continue

        price = item.get("price")
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            price = None

        items[asin] = {
            "pct": pct,
            "price": price,
            "fetched_at": fetched_at_raw,
            "src": "per_asin",
        }

    return items


def aggregate(
    per_asin_root: pathlib.Path | str,
    *,
    latest_path: pathlib.Path | str | None = None,
    min_pct: int = _MIN_PCT,
    stale_days: int = _STALE_DAYS,
    watch_stale_days: int = _WATCH_STALE_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """price_watch (優先) + per_asin (フォールバック) を集約し ASIN -> {pct, price, fetched_at, src} を返す。

    ``latest_path`` を省略した場合は price_watch を一切読まず、従来通り
    per_asin のみで集約する (呼び出し互換のため)。
    """
    now = now or datetime.now(timezone.utc)

    watch_items: dict[str, dict[str, Any]] = {}
    if latest_path is not None:
        watch_items = _load_price_watch_items(
            latest_path, min_pct=min_pct, watch_stale_days=watch_stale_days, now=now
        )

    per_asin_items = _aggregate_per_asin(
        per_asin_root,
        min_pct=min_pct,
        stale_days=stale_days,
        now=now,
        exclude=frozenset(watch_items.keys()),
    )

    # price_watch を優先 (同一 ASIN は price_watch 側の値で上書きしない=
    # per_asin 側は既に exclude 済みだが、念のため merge 順でも保証する)。
    items: dict[str, dict[str, Any]] = {**per_asin_items, **watch_items}

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {
            "min_pct": min_pct,
            "stale_days": stale_days,
            "watch_stale_days": watch_stale_days,
        },
        "count": len(items),
        "items": items,
    }


def write_output(payload: dict[str, Any], out_path: pathlib.Path | str) -> None:
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-asin-root", default=str(_DEFAULT_PER_ASIN_ROOT))
    p.add_argument("--latest", default=str(_DEFAULT_LATEST), help="data/price_watch/latest.json のパス")
    p.add_argument("--out", default=str(_DEFAULT_OUT))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    payload = aggregate(args.per_asin_root, latest_path=args.latest)
    write_output(payload, args.out)

    src_counts: dict[str, int] = {}
    for v in payload["items"].values():
        src = v.get("src", "unknown")
        src_counts[src] = src_counts.get(src, 0) + 1
    print(f"[build_discounts] count={payload['count']} src={src_counts} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
