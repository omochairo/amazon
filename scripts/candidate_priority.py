"""記事候補の優先度を計算する純粋関数群 (外部 API 呼び出しなし)。"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import unicodedata

import yaml

# 仮の値。asin_origin の記録で検証する
WEIGHTS = {
    "asin_b0h": 2, "asin_b0g": 1, "character": 2, "demand": 2,
    "brand_tier_sa": 1, "price_10000": 1, "similarity": -3,
}
DEFAULT_CHARACTER_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "character_terms.yaml"


def _fold(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-‐‑‒–—―・_.,/（）()]+", "", value)


def load_character_terms(path=DEFAULT_CHARACTER_PATH):
    """正規化キャラクター名 -> 表記別名。辞書が無ければ空辞書。"""
    try:
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    chars = data.get("characters", {}) if isinstance(data, dict) else {}
    return {str(k): tuple(str(x) for x in (v or [])) for k, v in chars.items()}


def match_character(item, terms):
    text = _fold(" ".join(str(item.get(k) or "") for k in ("title", "brand")))
    # 「デュプロ・アンパンマン互換の基礎板」のような互換品は、キャラクター商品ではない
    if "互換" in text:
        return None
    for canonical, aliases in (terms or {}).items():
        for alias in (canonical, *aliases):
            folded = _fold(alias)
            if folded and folded in text:
                return canonical
    return None


def brand_tier(brand):
    try:
        import brand_normalizer
        return brand_normalizer.normalize(brand).tier
    except Exception:
        return "D"


def browse_class(item):
    nodes = item.get("browse_nodes") or []
    return {str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")}


def existing_browse_counts(items, existing_asins, per_asin_root="data/raw/per_asin"):
    """既存記事のASINを商品スナップショットの browse node ごとに数える。"""
    by_asin = {i.get("asin"): i for i in items if isinstance(i, dict) and i.get("asin")}
    counts = {"__all__": len(existing_asins)}
    for asin in existing_asins:
        item = by_asin.get(asin)
        path = pathlib.Path(per_asin_root) / asin / "amazon.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            item = data.get("item") or item
        except (OSError, ValueError, AttributeError):
            pass
        for node in browse_class(item or {}):
            counts[node] = counts.get(node, 0) + 1
    return counts


def similar_candidate(item, *, lane, tier, character, existing_class_counts,
                      has_browse_nodes=True):
    if lane != "supply-random" or tier not in ("C", "D", "") or character:
        return False
    nodes = browse_class(item)
    if not has_browse_nodes or not nodes:
        return existing_class_counts.get("__all__", 0) >= 30
    return any(existing_class_counts.get(node, 0) >= 30 for node in nodes)


def score_candidate(item, *, existing_class_counts=None, character_terms=None,
                    has_browse_nodes=True):
    """候補を (合計点, 内訳, 正規化キャラクター名) で返す。"""
    item = item if isinstance(item, dict) else {}
    asin = str(item.get("asin") or "").upper()
    lane = item.get("lane")
    character = match_character(item, character_terms or {})
    # amazon.json には brand 欄が無い取得形式もあるため、既存 taxonomy の fuzzy
    # lookup に商品名も渡してブランド表記を拾う。
    tier = brand_tier(item.get("brand") or item.get("title"))
    try:
        price = float(item.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    breakdown = {
        "asin_prefix": WEIGHTS["asin_b0h"] if asin.startswith("B0H") else (WEIGHTS["asin_b0g"] if asin.startswith("B0G") else 0),
        "character": WEIGHTS["character"] if character else 0,
        "origin_demand": WEIGHTS["demand"] if lane == "demand" else 0,
        "brand_tier": WEIGHTS["brand_tier_sa"] if tier in ("S", "A") else 0,
        "price": WEIGHTS["price_10000"] if price >= 10000 else 0,
        "similarity": 0,
    }
    similar = similar_candidate(item, lane=lane, tier=tier, character=character,
                                existing_class_counts=existing_class_counts or {},
                                has_browse_nodes=has_browse_nodes)
    if similar:
        breakdown["similarity"] = WEIGHTS["similarity"]
    return sum(breakdown.values()), breakdown, character


def load_recent_character_counts(path="data/analytics/asin_origin.jsonl", *, now=None):
    """直近7日間の character 記録を正規化名ごとに数える。旧行は無視。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=7)
    counts = {}
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            row = json.loads(line)
            ts = dt.datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            char = row.get("character")
            if char and cutoff <= ts <= now:
                counts[char] = counts.get(char, 0) + 1
        except (ValueError, TypeError, AttributeError):
            continue
    return counts
