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

# `python -m scripts.<mod>` (package 形式) と `python scripts/<mod>.py`
# (scripts/ が sys.path に乗る形式) の両方で import できるようにする。
# scripts/ に __init__.py が無く両形式が混在しているため、素の兄弟 import は
# package 形式で ModuleNotFoundError になる。amazon-home-ops の lane は
# `python3 -m scripts.X --help` の可否をガードにしていて、失敗すると**緑のまま
# skip する**ので、この import 1 行で lane が無言で止まる (2026-08-12 の #5003 で
# quality_gate に `import stock_status` が入り、24-uniqueness-audit が 08-16 から
# 3 週間 skip し続けた)。
try:
    import price_overlay
except ModuleNotFoundError:  # package 形式
    from scripts import price_overlay  # type: ignore[no-redef]

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
# #5483: 発売前で予約を受け付けている状態。in_stock でも out_of_stock でもない。
# 「まだ発売していない」のに在庫ありと言うのも、予約できるのに在庫切れと言うのも
# 誤りなので、独立した状態にする。
STATE_PREORDER = "preorder"
STATE_UNKNOWN = "unknown"

# Amazon の「購入できる」とみなす在庫状態。delayed (発送に数日かかる) と
# preorder (発売日に届く) は購入操作そのものは可能なので available 扱いにする。
# unknown / out_of_stock は不可。
_AMAZON_AVAILABLE_STATES = frozenset(
    {STATE_IN_STOCK, STATE_LOW_STOCK, STATE_DELAYED, STATE_PREORDER}
)

# 「どこで買える」型タイトル + 在庫ブロックを適用してよい状態 (#5483)。
# preorder を外しているのは、発売前の商品に「どこで買える？在庫と価格を毎日
# チェック」という枠を当てても、読者に返せる中身が「まだ売っていない」しか
# 無いため。分類として正しく preorder と持つことと、その枠を当てることは別。
_STOCK_TITLE_STATES = frozenset(
    {STATE_IN_STOCK, STATE_LOW_STOCK, STATE_DELAYED, STATE_OUT_OF_STOCK}
)

_RE_LOW_STOCK = re.compile(r"残り\s*(\d+)\s*点")
# 「通常N〜N日以内に発送」に加えて 週間 / か月 も拾う (#5483)。実データに
# 「通常1～2か月以内に発送します。」が 3 件あり、日単位の正規表現から漏れて
# unknown に落ちていた。買えることに変わりはないので delayed が正しい。
_RE_DELAYED = re.compile(r"通常\d+[~〜～\-−]\d+(?:日|週間|か月|ヶ月|カ月)以内に発送")
# 「この商品の発売予定日は2026年9月19日です。」(#5483)
_RE_PREORDER = re.compile(r"発売予定日")
# 「一時的に在庫切れ」「この商品は現在お取り扱いできません。」(#5483)。
# どちらも買えない状態で、既存の「現在在庫切れです」と読者にとっての意味は同じ。
_RE_OUT_OF_STOCK = re.compile(r"在庫切れ|入荷時期は未定|お取り扱いできません|取り扱いできません")


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

    # #5483: 従来は `現在在庫切れです` で始まる文字列だけを out_of_stock にして
    # いたため、`一時的に在庫切れ; 入荷時期は未定です。` (17 件) と
    # `この商品は現在お取り扱いできません。` (6 件) が unknown に落ち、記事には
    # 「在庫状況は確認できませんでした」と出ていた。**在庫が無いと分かっている**
    # のに「分からない」と書くのは、#5130 で潰した「記録にないことを主張する」の
    # 裏返し (分かっていることを黙る) にあたる。
    if _RE_OUT_OF_STOCK.search(s):
        return STATE_OUT_OF_STOCK, None

    # 予約は out_of_stock より先に判定しない。`発売予定日` と在庫切れ文言が
    # 同時に出る形は実データに無いが、両方に当たったら「買えない」を優先する。
    if _RE_PREORDER.search(s):
        return STATE_PREORDER, None

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
        ``_STOCK_TITLE_STATES`` に入っている (unknown と preorder を除く)

    #5483: 判定を「unknown でない」から allowlist に変えた。分類が増えたときに
    「新しい state が黙って対象に入る」のを防ぐため。preorder を外している理由は
    ``_STOCK_TITLE_STATES`` のコメントを参照。

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
    return state in _STOCK_TITLE_STATES
