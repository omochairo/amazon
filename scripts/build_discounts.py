"""build_discounts.py

#3332 価格OFFバッジのサイト全面展開 — /deals/ 限定だった「定価からN%OFF」バッジを
トップページ各ブロック・ハブ・記事一覧・カルーセルの商品カードにも表示するための
ビルド時集約スクリプト。

``data/raw/per_asin/*/amazon.json`` (fetch_amazon.py が書く生データ。
``{"asin": str, "fetched_at": ISO, "item": {"savings_percentage": int, "price": int, ...}}``
形式) を走査し、閾値を満たす ASIN のみを ``hugo/data/discounts.json`` に書き出す。
front matter に依存せず、product_card.html 等の共有 partial (discount_badge.html)
が site.Data.discounts から ASIN 単位で lookup する。

採択条件:
  - ``item.savings_percentage >= _MIN_PCT``
  - ``fetched_at`` が ``_STALE_DAYS`` 日以内 (週次 fetch レーンなので古いスナップ
    ショットの割引率を出し続けない鮮度ガード)

``hugo/data/discounts.json`` は build_brand_hub_stats.py / build_price_dashboard.py
と同じ流儀で /hugo gitignore 配下のビルド時派生物であり commit しない。

CLI:
    python scripts/build_discounts.py
    python scripts/build_discounts.py --per-asin-root X --out Y
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
_DEFAULT_OUT = _REPO_ROOT / "hugo" / "data" / "discounts.json"

# ------------------------------------------------------------------------
# 較正可能なしきい値 (observation を見て後日調整する想定。
# build_price_dashboard.py の _DROP_PCT_THRESHOLD 等と同じ流儀)
# ------------------------------------------------------------------------
_MIN_PCT = 10       # この%未満の割引は採択しない (端数ノイズ・誤差除け)
_STALE_DAYS = 21    # fetched_at がこの日数より古いスナップショットは採択しない


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


def aggregate(
    per_asin_root: pathlib.Path | str,
    *,
    min_pct: int = _MIN_PCT,
    stale_days: int = _STALE_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """``data/raw/per_asin/*/amazon.json`` を走査し ASIN -> {pct, price, fetched_at} を返す。"""
    now = now or datetime.now(timezone.utc)
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
        }

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {"min_pct": min_pct, "stale_days": stale_days},
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
    p.add_argument("--out", default=str(_DEFAULT_OUT))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    payload = aggregate(args.per_asin_root)
    write_output(payload, args.out)
    print(f"[build_discounts] count={payload['count']} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
