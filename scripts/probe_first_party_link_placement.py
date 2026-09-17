"""probe_first_party_link_placement.py

#7569 型6 の probe 第2弾 (B): 「ASIN リンクの構造的な置き場所」に分離能があるかを **1 回測る**。

なぜ必要か:
  #7584 (手当て候補 (C) 「ASIN リンク近傍の一人称マーカー」) は却下された。理由は
  「効かなかった」ではなく、マーカー (地の文) とリンク (商品ボックス) が構造的に
  離れた場所にあるため N=200/500 では正例も負例もほぼ全部 0 だったこと。
  母艦の判定 (#7569 コメント3) はこの却下理由を根拠に、次に測るべきは
  「リンクが商品ボックス・比較表・ボタンの中にあるか、地の文の <p> 内にあるか」
  という **構造的な置き場所そのもの** だとした。本スクリプトはこれを 1 回測る。

設計判断:
  - **これは計測であって実装ではない**。本番の選別ロジック
    (`collect_first_party_sources.determine_roles` / `PRIMARY_SHARE_FLOOR` /
    `build_first_party_pool`) には一切配線しない
  - ラベル付き集合 (POSITIVE_ASINS / NEGATIVE_ASINS) は #7584 の
    `probe_first_party_link_proximity` からそのまま import する (定義を複製しない)
  - `collect_first_party_sources.py` / `probe_first_party_link_proximity.py` は
    どちらも変更しない (import するだけ)
  - `data/` には何も書かない・コミットしない。既定の出力先は `/tmp` 配下
  - 本文取得は `collect_first_party_sources.fetch_post_content` をそのまま使う
    (新しい取得系を書かない)。取得結果は `--cache` の JSON ファイルに永続化し、
    再実行では既にキャッシュ済みの post_id を叩き直さない
  - しきい値は提案しない。分布と「全正例を残すときに負例を何件落とせるか」
    「全負例を落とすときに正例が何件残るか」の 2 点だけを出す

カテゴリ定義 (実測 (#7569 コメント3 の指示どおり、まず実データを見てから決めた) 。
CATEGORY_DEFINITION_NOTES を参照):
  - heading_or_toc: 祖先タグに h1〜h6 を含む、または祖先の class に "toc" を含む文字列がある場合。
    実測 (66記事・599出現) では該当 0 件。理由は2つ: (1) WP REST の
    `content.rendered` は Cocoon のTOCブロックの HTML を含んでいない
    (テーマ側でページ描画時に別途注入される)。(2) ASIN リンクが見出し内に
    直接置かれる例が無かった。「無かった」こと自体が実測結果であり、
    分類ロジック自体は h1〜h6 と "toc" 系 class を見るように残す
  - block: 祖先タグ (リンク自身を含む) に <figure> / <table> がある、または
    class に次のいずれかの部分文字列を含む場合:
    "amazon-item" / "product-item" / "pochipp" / "shoplinkamazon" / "swatchimages"
    (Cocoon の商品ボックスウィジェットと pochipp プラグインの命名規則を実測して決めた)
  - narrative: 上記に該当せず、インライン要素 (a/strong/em/b/i/span) を飛ばして
    最初に現れるブロックレベル祖先が <p> であるもの
  - other: 上記いずれにも該当しないもの。実測で見つかった例:
    (1) `<div>` 3〜4 段のみ (class 無し、インライン style だけで実装された
    CTA ボタン。style 属性からの推測はしない方針のため block に入れず other 行き)
    (2) `<li>`/`<ul>` の中で box class を持たない出現 (実測では 0 件だったが、
    分類ロジックとしては起こり得るので other として残す)

使い方:
    python -m scripts.probe_first_party_link_placement --limit 5      # スモーク
    python -m scripts.probe_first_party_link_placement                # フル実行
    python -m scripts.probe_first_party_link_placement --dump-ancestors /tmp/ancestors.json
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import pathlib
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

from scripts.collect_first_party_sources import _ASIN_LINK_PATTERNS, fetch_post_content
from scripts.build_wp_navi_link_candidates import DEFAULT_SLEEP_SECONDS, DEFAULT_WP_BASE_URL
from scripts.probe_first_party_link_proximity import (
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    build_asin_posts,
    build_link_to_post_id,
    collect_post_ids,
    distribution_stats,
    full_drop_specificity,
    full_recall_specificity,
    write_json,
)
from scripts.probe_first_party_link_proximity import _now_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_first_party_link_placement")

DEFAULT_SOURCES_PATH = "data/analytics/first_party_sources.json"
DEFAULT_OUT = "/tmp/first_party_link_placement_probe.json"
DEFAULT_CACHE = "/tmp/first_party_link_placement_probe_cache.json"

CATEGORIES: tuple[str, ...] = ("narrative", "block", "heading_or_toc", "other")

_HEADING_TAG_RE = re.compile(r"^h[1-6]$")
_TOC_CLASS_MARKERS: tuple[str, ...] = ("toc",)
_BLOCK_TAGS: tuple[str, ...] = ("figure", "table")
_BLOCK_CLASS_MARKERS: tuple[str, ...] = (
    "amazon-item", "product-item", "pochipp", "shoplinkamazon", "swatchimages",
)
_INLINE_SKIP_TAGS: tuple[str, ...] = ("a", "strong", "em", "b", "i", "span")

CATEGORY_DEFINITION_NOTES: dict[str, str] = {
    "heading_or_toc": (
        "祖先タグに h1-h6、または祖先 class に 'toc' を含む文字列がある場合。"
        "実測 (66記事・599出現) では該当 0 件 (content.rendered に目次HTMLが含まれないため)。"
    ),
    "block": (
        "祖先 (リンク自身を含む) に <figure>/<table>、または class に "
        "amazon-item / product-item / pochipp / shoplinkamazon / swatchimages "
        "のいずれかの部分文字列を含む場合 (Cocoon 商品ボックス・pochippプラグインの命名規則)。"
    ),
    "narrative": (
        "上記に該当せず、インライン要素 (a/strong/em/b/i/span) を飛ばして"
        "最初に現れるブロックレベル祖先が <p> であるもの。"
    ),
    "other": (
        "上記いずれにも該当しないもの (class 無し・inline style のみの CTA ボタン等)。"
    ),
}


# --------------------------------------------------------------------------
# 祖先チェーンの抽出・分類 (pure functions。ネットワークを叩かない)
# --------------------------------------------------------------------------

def find_asin_link_tags(content_html: str, asin: str) -> list[Any]:
    """本文 HTML 中で、指定 ASIN のアフィリエイトリンク <a> タグを全部返す。"""
    if not isinstance(content_html, str) or not content_html:
        return []
    target = asin.upper()
    soup = BeautifulSoup(content_html, "html.parser")
    tags = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for pattern in _ASIN_LINK_PATTERNS:
            m = pattern.search(href)
            if m and m.group(1).upper() == target:
                tags.append(a)
                break
    return tags


def ancestor_chain(a_tag: Any) -> list[dict[str, Any]]:
    """<a> タグ自身から根まで、タグ名と class 属性の列を返す ([document] は含めない)。"""
    chain: list[dict[str, Any]] = []
    node = a_tag
    while node is not None and getattr(node, "name", None) not in (None, "[document]"):
        classes = node.get("class") if hasattr(node, "get") else None
        chain.append({"tag": node.name, "classes": list(classes) if classes else []})
        node = node.parent
    return chain


def classify_chain(chain: list[dict[str, Any]]) -> str:
    """祖先チェーンから排他的なカテゴリを1つ判定する。CATEGORY_DEFINITION_NOTES を参照。"""
    tags = [node["tag"] for node in chain]
    if any(_HEADING_TAG_RE.match(t) for t in tags):
        return "heading_or_toc"
    for node in chain:
        if any(marker in c for c in node["classes"] for marker in _TOC_CLASS_MARKERS):
            return "heading_or_toc"
    if any(t in _BLOCK_TAGS for t in tags):
        return "block"
    for node in chain:
        if any(marker in c for c in node["classes"] for marker in _BLOCK_CLASS_MARKERS):
            return "block"
    for node in chain:
        if node["tag"] in _INLINE_SKIP_TAGS:
            continue
        return "narrative" if node["tag"] == "p" else "other"
    return "other"


def categorize_occurrences(content_html: str, asin: str) -> list[dict[str, Any]]:
    """1 (post, asin) について、出現ごとの {category, chain} のリストを返す。"""
    tags = find_asin_link_tags(content_html, asin)
    out = []
    for a in tags:
        chain = ancestor_chain(a)
        out.append({"category": classify_chain(chain), "chain": chain})
    return out


# --------------------------------------------------------------------------
# 本文取得 (fetch_post_content を再利用。/tmp キャッシュで再取得を避ける)
# --------------------------------------------------------------------------

def load_cache(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path: pathlib.Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_contents_cached(
    post_ids: list[str],
    wp_base_url: str,
    session: requests.Session,
    sleep_seconds: float,
    limit: int,
    cache: dict[str, str],
    sleeper=time.sleep,
    fetch_fn=fetch_post_content,
) -> tuple[dict[str, str], list[str]]:
    """post_id -> 本文。キャッシュにあれば再取得しない。取得失敗した post_id は別リストで返す。"""
    ordered = sorted(post_ids, key=int)
    if limit and limit > 0:
        ordered = ordered[:limit]
    contents: dict[str, str] = {}
    failed: list[str] = []
    fetched_count = 0
    for pid in ordered:
        if pid in cache:
            contents[pid] = cache[pid]
            continue
        content = fetch_fn(int(pid), wp_base_url, session, sleeper=sleeper)
        if content is None:
            failed.append(pid)
        else:
            contents[pid] = content
            cache[pid] = content
        fetched_count += 1
        if fetched_count > 0 and sleep_seconds > 0 and pid != ordered[-1]:
            sleeper(sleep_seconds)
    return contents, failed


# --------------------------------------------------------------------------
# 測定・集計
# --------------------------------------------------------------------------

def build_pairs(
    asin_post_ids: dict[str, list[str]], contents: dict[str, str]
) -> list[dict[str, Any]]:
    """(asin, post_id) ごとに出現の {category, chain} リストを持つペア (本文取得済みのものだけ)。"""
    pairs = []
    for asin, pids in asin_post_ids.items():
        for pid in pids:
            content = contents.get(pid)
            if content is None:
                continue
            occurrences = categorize_occurrences(content, asin)
            pairs.append({"asin": asin, "post_id": pid, "occurrences": occurrences})
    return pairs


def aggregate_by_asin(
    pairs: list[dict[str, Any]], asins: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """asin -> カテゴリ別出現回数 (複数 post にまたがる場合は全ポストで畳む)。"""
    out: dict[str, dict[str, Any]] = {}
    for asin in asins:
        rows = [p for p in pairs if p["asin"] == asin]
        counts: collections.Counter = collections.Counter()
        for row in rows:
            counts.update(occ["category"] for occ in row["occurrences"])
        total = sum(counts.values())
        block_count = counts.get("block", 0)
        out[asin] = {
            "pair_count": len(rows),
            "total_occurrences": total,
            "category_counts": {c: counts.get(c, 0) for c in CATEGORIES},
            "block_count": block_count,
            "block_ratio": (block_count / total) if total else None,
            "narrative_only": (block_count == 0) if total > 0 else None,
        }
    return out


def boolean_breakdown(values: list[bool]) -> dict[str, int]:
    """真偽値の件数の分割表。"""
    return {"true": sum(1 for v in values if v), "false": sum(1 for v in values if not v)}


def category_crosstab(
    agg: dict[str, dict[str, Any]], asins: tuple[str, ...]
) -> dict[str, int]:
    """指定 ASIN 群についてカテゴリごとの出現回数の合計。"""
    totals: collections.Counter = collections.Counter()
    for asin in asins:
        for cat, cnt in agg[asin]["category_counts"].items():
            totals[cat] += cnt
    return {c: totals.get(c, 0) for c in CATEGORIES}


def build_report(
    agg: dict[str, dict[str, Any]],
    failed_post_ids: list[str],
    zero_link_pairs: int,
    total_pairs: int,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for kind in ("block_count", "block_ratio"):
        pos_values = [agg[a][kind] for a in POSITIVE_ASINS if agg[a][kind] is not None]
        neg_values = [agg[a][kind] for a in NEGATIVE_ASINS if agg[a][kind] is not None]
        metrics[kind] = {
            "positive": distribution_stats(pos_values),
            "negative": distribution_stats(neg_values),
            "full_recall_specificity": full_recall_specificity(pos_values, neg_values),
            "full_drop_specificity": full_drop_specificity(pos_values, neg_values),
            "positive_missing": len(POSITIVE_ASINS) - len(pos_values),
            "negative_missing": len(NEGATIVE_ASINS) - len(neg_values),
        }

    pos_narrative = [agg[a]["narrative_only"] for a in POSITIVE_ASINS if agg[a]["narrative_only"] is not None]
    neg_narrative = [agg[a]["narrative_only"] for a in NEGATIVE_ASINS if agg[a]["narrative_only"] is not None]
    metrics["narrative_only"] = {
        "positive": boolean_breakdown(pos_narrative),
        "negative": boolean_breakdown(neg_narrative),
        "positive_missing": len(POSITIVE_ASINS) - len(pos_narrative),
        "negative_missing": len(NEGATIVE_ASINS) - len(neg_narrative),
    }

    return {
        "generated_at": _now_iso(),
        "category_definitions": CATEGORY_DEFINITION_NOTES,
        "positive_asins": list(POSITIVE_ASINS),
        "negative_asins": list(NEGATIVE_ASINS),
        "fetch_failed_posts": failed_post_ids,
        "zero_link_pairs": zero_link_pairs,
        "total_pairs": total_pairs,
        "metrics": metrics,
        "category_crosstab": {
            "positive": category_crosstab(agg, POSITIVE_ASINS),
            "negative": category_crosstab(agg, NEGATIVE_ASINS),
        },
        "per_asin": agg,
    }


def build_ancestor_dump(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """ステップ1成果物: 祖先チェーンの実測一覧と、祖先タグ/class の出現頻度集計。"""
    occurrences = []
    tag_freq: collections.Counter = collections.Counter()
    class_freq: collections.Counter = collections.Counter()
    for pair in pairs:
        for occ in pair["occurrences"]:
            occurrences.append({
                "asin": pair["asin"],
                "post_id": pair["post_id"],
                "category": occ["category"],
                "chain": occ["chain"],
            })
            for node in occ["chain"]:
                tag_freq[node["tag"]] += 1
                for cls in node["classes"]:
                    class_freq[cls] += 1
    return {
        "occurrences": occurrences,
        "tag_frequency": dict(tag_freq.most_common()),
        "class_frequency": dict(class_freq.most_common()),
    }


def run(
    *,
    sources_path: pathlib.Path,
    out_path: pathlib.Path,
    cache_path: pathlib.Path,
    dump_ancestors_path: pathlib.Path | None = None,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    fetch_fn=fetch_post_content,
) -> dict[str, Any]:
    session = session or requests.Session()
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    sources = payload.get("sources") or []
    posts_cache = payload.get("posts_cache") or {}

    all_asins = set(POSITIVE_ASINS) | set(NEGATIVE_ASINS)
    asin_posts = build_asin_posts(sources, all_asins)
    link_to_id = build_link_to_post_id(posts_cache)
    asin_post_ids = collect_post_ids(asin_posts, link_to_id)

    unique_ids = sorted({pid for ids in asin_post_ids.values() for pid in ids}, key=int)
    logger.info("対象 post: %d 件 (正例%d + 負例%d ASIN)", len(unique_ids),
                len(POSITIVE_ASINS), len(NEGATIVE_ASINS))

    cache = load_cache(cache_path)
    contents, failed_ids = fetch_contents_cached(
        unique_ids, wp_base_url, session, sleep_seconds, limit, cache,
        sleeper=sleeper, fetch_fn=fetch_fn,
    )
    save_cache(cache_path, cache)
    if failed_ids:
        logger.warning("本文取得に失敗した post: %d 件 %s", len(failed_ids), failed_ids)

    pairs = build_pairs(asin_post_ids, contents)
    zero_link_pairs = sum(1 for p in pairs if not p["occurrences"])

    agg = aggregate_by_asin(pairs, POSITIVE_ASINS + NEGATIVE_ASINS)
    report = build_report(agg, failed_ids, zero_link_pairs, len(pairs))

    write_json(out_path, report)
    logger.info("wrote %s (pairs=%d, zero_link_pairs=%d, fetch_failed=%d)",
                out_path, len(pairs), zero_link_pairs, len(failed_ids))

    if dump_ancestors_path is not None:
        write_json(dump_ancestors_path, build_ancestor_dump(pairs))
        logger.info("wrote %s (occurrences=%d)", dump_ancestors_path,
                    sum(len(p["occurrences"]) for p in pairs))

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=DEFAULT_SOURCES_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="本文取得結果の /tmp キャッシュ")
    ap.add_argument("--dump-ancestors", default=None, help="祖先チェーン実測一覧の出力先 (省略時は出さない)")
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    ap.add_argument("--limit", type=int, default=0, help="取得する post 数の上限 (スモーク用, 0=無制限)")
    args = ap.parse_args()
    run(
        sources_path=pathlib.Path(args.sources),
        out_path=pathlib.Path(args.out),
        cache_path=pathlib.Path(args.cache),
        dump_ancestors_path=pathlib.Path(args.dump_ancestors) if args.dump_ancestors else None,
        wp_base_url=args.wp_base_url,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
