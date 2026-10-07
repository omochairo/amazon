"""商品画像 URL を検証済み ``amazon.json`` から解決する共通関数。

#2812 で記事ページ (build_post.py ``_enforce_amazon_image``) は
``data/raw/per_asin/<ASIN>/amazon.json`` の画像で ``product.image`` を強制上書き
するようになったが、``data/articles/*.json`` を直接読む集計 (/deals/ /cospa/・
カテゴリ/年齢ハブ・ブランドハブ・値下がりダッシュボード・OG 画像・記事内の
関連商品カード) は Jules の生値をそのまま出していた。Jules は画像 URL を
捏造することがあり (例 B0DF72LSP7 の ``71xyz123abc._AC_SX679_.jpg``、
B0BT4NSYBX の ``dummy.jpg``)、記事ページは正しいのに /deals/ だけ画像が
壊れる、という食い違いが起きた。

優先順位は ``_enforce_amazon_image`` と同じ: ``item.image`` → ``item.images[0]``
→ (amazon.json に画像が無ければ) 記事 JSON の値。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PER_ASIN_ROOT = _REPO_ROOT / "data" / "raw" / "per_asin"


def amazon_image(item: Any) -> str:
    """amazon.json の ``item`` dict から検証済み画像 URL を返す (無ければ "")。"""
    if not isinstance(item, dict):
        return ""
    img = item.get("image")
    if isinstance(img, str) and img:
        return img
    images = item.get("images")
    if isinstance(images, list):
        for u in images:
            if isinstance(u, str) and u:
                return u
    return ""


def load_amazon_image(per_asin_root: Path | str | None, asin: str | None) -> str:
    """``<per_asin_root>/<ASIN>/amazon.json`` の画像 URL を返す (無い・壊れていれば "")。

    ``{asin, fetched_at, item}`` 形と、item を root に置いた旧形の両方を受ける。
    """
    key = asin.strip().upper() if isinstance(asin, str) else ""
    if not key:
        return ""
    root = DEFAULT_PER_ASIN_ROOT if per_asin_root is None else Path(per_asin_root)
    path = root / key / "amazon.json"
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(snap, dict):
        return ""
    item = snap.get("item") if isinstance(snap.get("item"), dict) else snap
    return amazon_image(item)


def resolve_product_image(
    asin: str | None,
    fallback: Any,
    per_asin_root: Path | str | None = None,
) -> str:
    """amazon.json の画像を優先し、無ければ記事 JSON の値 (``fallback``) を返す。"""
    img = load_amazon_image(per_asin_root, asin)
    if img:
        return img
    return fallback if isinstance(fallback, str) else ""
