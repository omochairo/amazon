"""価格欄 (build_post._resolve_buy_offer) と知育スコアの内訳 (score_calculator) が
同じ判定を使うための、サイト別価格エントリの小さな判定関数。

score_calculator から import されるので、build_post など重い依存を持たせないこと
(build_post -> score_calculator -> 本モジュール、の一方向)。
"""
from __future__ import annotations

from typing import Any, Optional

# (key, ボタンに出す名前, 導入文で使う短い名前)。並びは価格カードと同じで、同額のときは前を採る。
BUY_SITES = (
    ("amazon", "Amazon", "Amazon"),
    ("rakuten", "楽天市場", "楽天"),
    ("yahoo", "Yahoo!ショッピング", "Yahoo!ショッピング"),
)


def amazon_stock_state(availability: Any) -> str:
    """Amazon PA-API の availability メッセージから在庫状態を分類する。

    返り値: "instock" / "low_stock" / "outofstock" / "preorder" / "unknown"
    楽天/Yahoo は fetch_cross_search.py で正規化済 (instock/outofstock) だが
    Amazon は自由記述文字列のためテンプレ側で分類する必要がある。
    """
    if not availability or not isinstance(availability, str):
        return "unknown"
    text = availability.strip()
    if not text:
        return "unknown"
    if "在庫切れ" in text or "入荷時期は未定" in text or "取扱い終了" in text:
        return "outofstock"
    if "発売予定日" in text or "予約受付" in text:
        return "preorder"
    if "残り" in text and "点" in text:
        return "low_stock"
    if "在庫あり" in text or text.startswith("通常"):
        return "instock"
    return "unknown"


def entry_price(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("price") or 0)
    except (TypeError, ValueError):
        return 0


def entry_out_of_stock(key: str, entry: dict[str, Any]) -> bool:
    if key == "amazon":
        return amazon_stock_state(entry.get("availability")) == "outofstock"
    return entry.get("availability") == "outofstock"


def entry_is_direct(entry: dict[str, Any]) -> bool:
    """ボタンの行き先にできる出品か (url あり・検索リンクでない・販売終了でない・同一商品と未確認でない)。"""
    return (bool(entry.get("url")) and not entry.get("is_search") and not entry.get("discontinued")
            and entry.get("verified") is not False)


def entry_is_priced(entry: dict[str, Any]) -> bool:
    """価格カードに金額が出るか (price>0 かつ販売終了でない)。"""
    return entry_price(entry) > 0 and not entry.get("discontinued")


def priced_site_keys(prices: dict[str, Any]) -> list[str]:
    """価格が出るサイトの key (価格欄の priced と同じ。verified=False も数える)。"""
    return [key for key, _l, _s in BUY_SITES
            if isinstance(prices.get(key), dict) and entry_is_priced(prices[key])]


def cheapest_purchasable(prices: dict[str, Any], *, include_out_of_stock: bool = False
                         ) -> Optional[tuple[int, str]]:
    """購入できる最安 (price, key)。価格欄の主ボタンの eligible と同じ条件。

    include_out_of_stock=True のときは在庫切れも候補に入れる。
    """
    cands: list[tuple[int, int, str]] = []
    for order, (key, _l, _s) in enumerate(BUY_SITES):
        e = prices.get(key)
        if not isinstance(e, dict):
            continue
        p = entry_price(e)
        if p > 0 and entry_is_direct(e) and (include_out_of_stock or not entry_out_of_stock(key, e)):
            cands.append((p, order, key))
    if not cands:
        return None
    p, _o, key = min(cands)
    return p, key
