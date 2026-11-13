"""おもちゃの「種類」を判定する (navi-brain#165 T2)。

関連のおすすめ (build_post の 🆚 似た商品 / 💴 同じ価格帯 / 🌱 半年〜1年後、
Hugo の 📚 関連) で、種類が違う商品を出さないための判定。ルールは
``data/toy_types.yaml`` にある (キーワードの理由もそちら)。

判定は 3 段で、最初に種類が見つかった段で決める:
  1. Amazon のカテゴリ名 (per_asin の amazon.json の ``browse_nodes``)
  2. 記事のタグ
  3. 商品名
1 つの商品に複数の種類が付いてよい。どこでも見つからなければ空 (= 種類不明)。
種類不明は「違う」とはみなさない (呼び出し側で 2 段目の候補として扱う)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from genre_gate import STORE_NODE_IDS

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "toy_types.yaml"

# 名前がノード ID で始まるカテゴリ (例 "2189078051,2189163051" / "2189602051 game") は
# 売り場の内部用。「3Dパズル」のような先頭が数字の普通の名前は外さないよう、6 桁以上で見る
_ID_LIKE_RE = re.compile(r"^\d{6,}")


@dataclass(frozen=True)
class ToyTypeRules:
    keys: tuple[str, ...]
    labels: dict[str, str]
    keywords: dict[str, tuple[str, ...]]
    ignore_node_substrings: tuple[str, ...]
    ignore_node_names: frozenset[str]


@lru_cache(maxsize=None)
def load_rules(path: str | None = None) -> ToyTypeRules:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    keys: list[str] = []
    labels: dict[str, str] = {}
    keywords: dict[str, tuple[str, ...]] = {}
    for t in raw.get("types") or []:
        key = str(t["key"])
        keys.append(key)
        labels[key] = str(t.get("label") or key)
        keywords[key] = tuple(str(k).lower() for k in (t.get("keywords") or []) if str(k).strip())
    return ToyTypeRules(
        keys=tuple(keys),
        labels=labels,
        keywords=keywords,
        ignore_node_substrings=tuple(str(s).lower() for s in raw.get("ignore_node_substrings") or []),
        ignore_node_names=frozenset(str(s) for s in raw.get("ignore_node_names") or []),
    )


def browse_node_names(amazon_item: dict[str, Any] | None, rules: ToyTypeRules | None = None) -> list[str]:
    """amazon.json の item から、種類の判定に使うカテゴリ名だけを返す。

    売り場・特集・テスト用のノードと、複数の種類にまたがる大分類は外す。
    """
    if not isinstance(amazon_item, dict):
        return []
    nodes = amazon_item.get("browse_nodes")
    if not isinstance(nodes, list):
        return []
    rules = rules or load_rules()
    out: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if str(n.get("id") or "") in STORE_NODE_IDS:
            continue
        name = str(n.get("name") or "").strip()
        if not name or name in rules.ignore_node_names or _ID_LIKE_RE.match(name):
            continue
        low = name.lower()
        if any(s in low for s in rules.ignore_node_substrings):
            continue
        out.append(name)
    return out


def _hit(texts: Iterable[str], rules: ToyTypeRules) -> set[str]:
    found: set[str] = set()
    for text in texts:
        low = str(text).lower()
        for key in rules.keys:
            if key not in found and any(kw in low for kw in rules.keywords[key]):
                found.add(key)
    return found


def classify(
    node_names: Iterable[str],
    tags: Iterable[str],
    name: str,
    rules: ToyTypeRules | None = None,
) -> tuple[list[str], str]:
    """(種類のキーの一覧 (YAML の順), 決めた段 "node" / "tag" / "name" / "none") を返す。"""
    rules = rules or load_rules()
    for source, texts in (
        ("node", list(node_names)),
        ("tag", [t for t in tags if isinstance(t, str)]),
        ("name", [name] if name else []),
    ):
        found = _hit(texts, rules)
        if found:
            return [k for k in rules.keys if k in found], source
    return [], "none"


def classify_article(data: dict[str, Any], amazon_item: dict[str, Any] | None) -> list[str]:
    """記事 JSON と amazon.json の item から種類を判定する。"""
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    tags = [t for t in (data.get("tags") or []) if isinstance(t, str)]
    types, _ = classify(browse_node_names(amazon_item), tags, str(product.get("name") or ""))
    return types


def label(key: str) -> str:
    return load_rules().labels.get(key, key)
