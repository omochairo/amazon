"""stock_status.py

#2686 / #4964 — 「どこで買える/在庫」記事型のためのデータ解決層。

このモジュールは **純粋なデータ解決** のみを行う。記事生成 (build_post.py /
templates/post.md.j2) の出力には一切触れない。後続 PR でタイトル生成の
ゲートとして使う想定。

背景・データ源 (実測・2026-08-12):
  ``data/price_watch/latest.json``::

      {"generated_at": "2026-08-11T21:12:37.612371+00:00",
       "items": {"<ASIN>": {"p": 3149, "off": 26, "ts": "2026-08-11T21:07:45+00:00",
                             "avail": "在庫あり。"}}}

  - ``off`` (割引率%) は 0 のときキー自体が無い
  - ``avail`` も欠けることがある
  - ``p`` (価格) が無い ASIN もある (実測 93.5% しか価格が無い)。
    price_overlay.load_watch_index は価格が取れない entry を index に
    採択しない (=「price が無い観測は使わない」設計) ため、在庫文言
    (``avail``) だけを見たい本モジュールは price_watch の生 JSON を
    別途読む。**価格解決そのものは price_overlay.resolve() に委譲し、
    重複実装しない。**

  ``avail`` の実分布 (上位・全 1,874 件中):
    在庫あり。 1,236 / 現在在庫切れです。 115 / 残り1点 ご注文はお早めに 31 /
    通常4～5日以内に発送します。 28 / 残り3点 ご注文はお早めに 22 /
    残り2点 ご注文はお早めに 22 / 残り10点 ご注文はお早めに 20 /
    通常2～3日以内に発送します。 20 / 残り12点 ご注文はお早めに 17 /
    残り4点 ご注文はお早めに 15

  記事側の楽天/Yahoo 価格は ``data/articles/*.json`` の ``product.prices``
  にある::

      {"amazon": {"price": int, "url": str},
       "rakuten": {"price": int, "url": str, "is_search": bool},
       "yahoo": {"price": int, "url": str, "is_search": bool}}

  ``price`` が 0 かつ ``is_search`` が true は「取扱を確認できなかった
  (検索リンクへのフォールバック)」を意味する。

Pure aggregation only — 外部 API 呼び出しなし・CLI なし・data/ 書き込みなし。
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import price_overlay

logger = logging.getLogger("stock_status")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_LATEST = _REPO_ROOT / "data" / "price_watch" / "latest.json"

# ------------------------------------------------------------------------
# 在庫状態
# ------------------------------------------------------------------------
STATE_IN_STOCK = "in_stock"
STATE_LOW_STOCK = "low_stock"
STATE_OUT_OF_STOCK = "out_of_stock"
STATE_DELAYED = "delayed"
STATE_UNKNOWN = "unknown"

# Amazon の「購入できる」とみなす在庫状態。delayed (発送に数日かかる) は
# 購入自体は可能なので available 扱いにする。unknown / out_of_stock は不可。
_AMAZON_AVAILABLE_STATES = frozenset({STATE_IN_STOCK, STATE_LOW_STOCK, STATE_DELAYED})

_RE_LOW_STOCK = re.compile(r"残り\s*(\d+)\s*点")
_RE_DELAYED = re.compile(r"通常\d+[~〜～\-−]\d+日以内に発送")


def classify_availability(raw_avail: Any) -> tuple[str, Optional[int]]:
    """``avail`` の生文言を (state, remaining) に分類する。

    **未知の文言 / 欠落は unknown に落とし、in_stock などへ推測で倒さない。**
    未知パターンは後から分布を確認できるよう logger.info に出す。
    """
    if not isinstance(raw_avail, str):
        return STATE_UNKNOWN, None
    s = raw_avail.strip()
    if not s:
        return STATE_UNKNOWN, None

    m = _RE_LOW_STOCK.search(s)
    if m:
        return STATE_LOW_STOCK, int(m.group(1))

    if s.startswith("在庫あり"):
        return STATE_IN_STOCK, None

    if s.startswith("現在在庫切れです"):
        return STATE_OUT_OF_STOCK, None

    if _RE_DELAYED.search(s):
        return STATE_DELAYED, None

    logger.info(f"stock_status: unknown avail pattern, falling back to unknown: {s!r}")
    return STATE_UNKNOWN, None


def _load_json(path: pathlib.Path | str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"skip unreadable/invalid json {path}: {e}")
        return None
    return d if isinstance(d, dict) else None


@dataclass(frozen=True)
class StockObservation:
    """1 ASIN 分の在庫・価格観測点。"""

    asin: str
    state: str  # in_stock / low_stock / out_of_stock / delayed / unknown
    remaining: int | None
    raw_avail: str | None
    price: int | None
    observed_at: str | None


@dataclass(frozen=True)
class StockIndex:
    """``data/price_watch/latest.json`` を 1 回読んで使い回すための index。

    ``items`` は在庫文言 (``avail``) を読むための生 entry (ASIN -> dict、
    価格の有無に関わらず全件保持)。``price_index`` は price_overlay 側の
    価格 index (価格が取れる entry のみ) をそのまま持つ。
    """

    items: dict[str, dict]
    generated_at: str | None
    price_index: dict[str, price_overlay.PriceObservation]


def load_stock_index(
    latest_path: pathlib.Path | str | None = None,
    *,
    price_stale_days: int = price_overlay.DEFAULT_WATCH_STALE_DAYS,
    now: Optional[datetime] = None,
) -> StockIndex:
    """price_watch の latest.json から在庫 index を作る。

    無い/壊れている場合は空の index を返し、呼び出し側は unknown /
    fail-soft で扱えるようにする。
    """
    path = _DEFAULT_LATEST if latest_path is None else latest_path

    d = _load_json(path)
    items: dict[str, dict] = {}
    generated_at: str | None = None
    if d is not None:
        ga = d.get("generated_at")
        generated_at = ga if isinstance(ga, str) and ga.strip() else None
        raw_items = d.get("items")
        if isinstance(raw_items, dict):
            for asin, entry in raw_items.items():
                if not isinstance(asin, str) or not asin.strip():
                    continue
                if not isinstance(entry, dict):
                    continue
                items[asin.strip().upper()] = entry

    price_index = price_overlay.load_watch_index(path, stale_days=price_stale_days, now=now)

    return StockIndex(items=items, generated_at=generated_at, price_index=price_index)


def resolve_stock(
    asin: str,
    index: StockIndex,
    *,
    per_asin_root: pathlib.Path | str | None = None,
    now: Optional[datetime] = None,
) -> StockObservation:
    """1 ASIN の在庫・価格観測を解決する。

    - 在庫文言 (avail/remaining/state) は price_watch の生 entry から。
    - 価格は price_overlay.resolve() に委譲する (price_watch 優先 →
      per_asin フォールバック。重複実装しない)。
    - observed_at は entry の ``ts``、無ければ index 全体の
      ``generated_at`` にフォールバックする。
    """
    key = asin.strip().upper() if isinstance(asin, str) and asin.strip() else ""
    if not key:
        return StockObservation(
            asin="", state=STATE_UNKNOWN, remaining=None, raw_avail=None,
            price=None, observed_at=None,
        )

    entry = index.items.get(key)
    raw_avail: str | None = None
    ts: str | None = None
    if entry is not None:
        av = entry.get("avail")
        raw_avail = av if isinstance(av, str) and av.strip() else None
        ts_raw = entry.get("ts")
        ts = ts_raw if isinstance(ts_raw, str) and ts_raw.strip() else None

    state, remaining = classify_availability(raw_avail)

    obs = price_overlay.resolve(
        key, watch_index=index.price_index, per_asin_root=per_asin_root, now=now,
    )
    price = obs.price if obs is not None else None

    observed_at = ts if ts is not None else index.generated_at

    return StockObservation(
        asin=key, state=state, remaining=remaining, raw_avail=raw_avail,
        price=price, observed_at=observed_at,
    )


# ------------------------------------------------------------------------
# 販売先サマリ
# ------------------------------------------------------------------------

def resolve_purchase_options(
    product_prices: dict | None,
    stock_obs: StockObservation,
) -> dict[str, dict]:
    """記事の ``product.prices`` + 在庫観測から「どこで買えるか」を返す。

    サイトごとに ``{available, price, url, is_search}``。Amazon は
    price_watch の在庫状態 (``stock_obs.state``) を優先して available を
    決める。楽天/Yahoo は ``price > 0`` を「取扱あり」、``is_search`` は
    「取扱を確認できず」とする。
    """
    prices = product_prices if isinstance(product_prices, dict) else {}

    amazon_entry = prices.get("amazon")
    amazon_url = None
    if isinstance(amazon_entry, dict):
        u = amazon_entry.get("url")
        amazon_url = u if isinstance(u, str) and u.strip() else None

    result: dict[str, dict] = {
        "amazon": {
            "available": stock_obs.state in _AMAZON_AVAILABLE_STATES,
            "price": stock_obs.price,
            "url": amazon_url,
            "is_search": False,
        }
    }

    for site in ("rakuten", "yahoo"):
        entry = prices.get(site)
        if not isinstance(entry, dict):
            result[site] = {"available": False, "price": None, "url": None, "is_search": False}
            continue

        raw_price = entry.get("price")
        price = raw_price if isinstance(raw_price, int) and not isinstance(raw_price, bool) else None
        is_search = bool(entry.get("is_search"))
        url_raw = entry.get("url")
        url = url_raw if isinstance(url_raw, str) and url_raw.strip() else None

        has_price = price is not None and price > 0
        available = has_price and not is_search

        result[site] = {
            "available": available,
            "price": price if has_price else None,
            "url": url,
            "is_search": is_search,
        }

    return result


# ------------------------------------------------------------------------
# 新型タイトルの適用可否
# ------------------------------------------------------------------------

def can_use_stock_title(asin: str, index: StockIndex) -> bool:
    """「どこで買える」型タイトルを使ってよいかを判定する。

    次を全て満たすときだけ True:
      - 当該 ASIN が price_watch (``index.items``) に存在する
      - ``avail`` が取れており、classify_availability の state が
        unknown でない

    価格の有無は問わない (在庫文言だけを見るゲート)。この関数はフラグを
    返すだけで、記事出力には一切使わない (呼び出し側=後続 PR の責務)。
    """
    key = asin.strip().upper() if isinstance(asin, str) and asin.strip() else ""
    if not key:
        return False

    entry = index.items.get(key)
    if entry is None:
        return False

    av = entry.get("avail")
    raw_avail = av if isinstance(av, str) and av.strip() else None
    state, _ = classify_availability(raw_avail)
    return state != STATE_UNKNOWN
