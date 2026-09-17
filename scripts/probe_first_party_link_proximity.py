"""probe_first_party_link_proximity.py

#7569 型6 の追測: 「ASIN リンク近傍の一人称マーカー」に分離能があるかを **1 回測る**。

なぜ必要か:
  `collect_first_party_sources.py` の `fp_markers` は post 単位の値で、同じ記事に
  出る全 ASIN で同一になる。そのため「記事がレビューしている商品」と「記事が
  引用しただけの商品」を区別できない (#7569 型6)。手当て候補 (C) は
  「一人称マーカーを ASIN リンクの前後 N 文字で数えれば分離できるのでは」という
  仮説で、既存のラベル付き集合 1 回でこれを測る (#7569 コメント2)。

設計判断:
  - **これは計測であって実装ではない**。本番の選別ロジック (`determine_roles` /
    `PRIMARY_SHARE_FLOOR` / `build_first_party_pool`) には一切配線しない
  - `data/` には何も書かない・コミットしない。既定の出力先は `/tmp` 配下
  - 本文取得は `collect_first_party_sources.fetch_post_content` をそのまま使う
    (新しい取得系を書かない)。post_id は `data/analytics/first_party_sources.json`
    の `posts_cache` (post_url -> post_id) から引く
  - `FP_MARKERS` の中身は増やさない。信号の粒度 (post 単位 -> リンク近傍) だけを変える
  - しきい値は提案しない。分布と「全正例を残すときに負例を何件落とせるか」
    「全負例を落とすときに正例が何件残るか」の 2 点だけを出す

使い方:
    python -m scripts.probe_first_party_link_proximity --limit 5   # スモーク (post 5件だけ取得)
    python -m scripts.probe_first_party_link_proximity              # フル実行 (post 66件前後)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.collect_first_party_sources import (
    FP_MARKERS,
    _ASIN_LINK_PATTERNS,
    fetch_post_content,
)
from scripts.build_wp_navi_link_candidates import (
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_WP_BASE_URL,
    strip_html,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_first_party_link_proximity")

DEFAULT_SOURCES_PATH = "data/analytics/first_party_sources.json"
DEFAULT_OUT = "/tmp/first_party_link_proximity_probe.json"
WINDOW_SIZES: tuple[int, ...] = (200, 500, 1000)

# 正例 (#7568 で母艦が採用した experience.json 7 件 + 題名から (i) と判断した書籍 5 件)
POSITIVE_ASINS: tuple[str, ...] = (
    "B00RZ4LM74", "B073W9V2WB", "B07RW6NNZV", "B0BD3BYQKY", "B0C3LN76K7",
    "B0C42KVVG8", "B0DBTLH8ZM",
    "4057507701", "4097353594", "4756255523", "4591156168", "409253597X",
)

# 負例 (#7569 コメント2「B0 側 159 件のうち 52 件 (32.7%) が (ii)」の母集団)
NEGATIVE_ASINS: tuple[str, ...] = (
    "B00D3UOBHC", "B00DTM2DGK", "B00HJFR9TY", "B00ZF3P72S", "B00ZQFPQLW",
    "B0722JMZYD", "B075CG2WJP", "B07KM2DSY5", "B07PXX1J6J", "B0834Y75Z6",
    "B08BNRF1PN", "B094V5PHB9", "B095Q2GDVR", "B099MTRBD4", "B09B2RLPLV",
    "B09B9TBZ71", "B09FF61N5V", "B09TVWQ2R8", "B09ZHF2MPM", "B0BDK3Y4HR",
    "B0BDR27DBY", "B0BL5PN9MG", "B0BLS56CT4", "B0BPB8FNV1", "B0BVY5B2Q4",
    "B0BW2VCPTG", "B0C61QTWM3", "B0C7GY8PJP", "B0CCJ35T1Z", "B0CG5PLGFN",
    "B0CKYM15RJ", "B0CMFRVGW9", "B0CTMM93J5", "B0CW1BQ7GH", "B0D419WLT4",
    "B0D9JL38DJ", "B0DHVD9XLQ", "B0DJSBSCM9", "B0DMS5ZFH3", "B0DNDHHWZL",
    "B0DPLRLQM7", "B0DSGRVSLJ", "B0DSL587CP", "B0DT9BBST1", "B0F54NFW3Y",
    "B0F5W9BS59", "B0F7451FXW", "B0FBWM1QPS", "B0FHHDBDPT", "B0FQMTN7CP",
    "B0FW4QNBHL", "B0H3T1WFDV",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 窓の切り出し・マーカー計数 (pure functions。ネットワークを叩かない)
# --------------------------------------------------------------------------

def find_asin_link_spans(content_html: str, asin: str) -> list[tuple[int, int]]:
    """本文 HTML 中で、指定 ASIN のアフィリエイトリンク出現位置 (start, end) を全部返す。"""
    if not isinstance(content_html, str) or not content_html:
        return []
    target = asin.upper()
    spans: list[tuple[int, int]] = []
    for pattern in _ASIN_LINK_PATTERNS:
        for m in pattern.finditer(content_html):
            if m.group(1).upper() == target:
                spans.append(m.span())
    return spans


def extract_windows(content_html: str, asin: str, window_chars: int) -> list[str]:
    """各リンク出現の前後 window_chars 文字を切り出し、strip_html したテキストのリスト。"""
    spans = find_asin_link_spans(content_html, asin)
    n = len(content_html)
    windows = []
    for start, end in spans:
        lo = max(0, start - window_chars)
        hi = min(n, end + window_chars)
        windows.append(strip_html(content_html[lo:hi]))
    return windows


def count_markers_in_text(text: str) -> int:
    """既に可視テキスト化済みの文字列中の一人称マーカー出現回数の合計。"""
    return sum(text.count(marker) for marker in FP_MARKERS)


def score_asin_in_post(content_html: str, asin: str, window_chars: int) -> dict[str, int]:
    """1 (post, asin) について窓ごとのマーカー数から occurrences/max/sum を出す。"""
    windows = extract_windows(content_html, asin, window_chars)
    counts = [count_markers_in_text(w) for w in windows]
    return {
        "occurrences": len(counts),
        "max": max(counts) if counts else 0,
        "sum": sum(counts) if counts else 0,
    }


# --------------------------------------------------------------------------
# ラベル付き集合 <-> post_id の対応付け
# --------------------------------------------------------------------------

def build_asin_posts(sources: list[dict[str, Any]], asins: set[str]) -> dict[str, list[str]]:
    """asin -> role==primary の post_url リスト (重複除去・出現順)。"""
    out: dict[str, list[str]] = {}
    for row in sources:
        asin = row.get("asin")
        if row.get("role") != "primary" or asin not in asins:
            continue
        urls = out.setdefault(asin, [])
        url = row.get("post_url")
        if url and url not in urls:
            urls.append(url)
    return out


def build_link_to_post_id(posts_cache: dict[str, Any]) -> dict[str, str]:
    """post_url -> post_id (posts_cache の link から逆引き)。"""
    out: dict[str, str] = {}
    for pid, entry in posts_cache.items():
        if not isinstance(entry, dict):
            continue
        link = entry.get("link")
        if isinstance(link, str) and link:
            out[link] = pid
    return out


def collect_post_ids(asin_posts: dict[str, list[str]], link_to_id: dict[str, str]) -> dict[str, list[str]]:
    """asin -> post_id リスト (post_url が posts_cache に無ければ落とす)。"""
    out: dict[str, list[str]] = {}
    for asin, urls in asin_posts.items():
        ids = []
        for url in urls:
            pid = link_to_id.get(url)
            if pid and pid not in ids:
                ids.append(pid)
        out[asin] = ids
    return out


# --------------------------------------------------------------------------
# 本文取得 (fetch_post_content をそのまま使う。ここでは呼び出しだけ)
# --------------------------------------------------------------------------

def fetch_contents(
    post_ids: list[str],
    wp_base_url: str,
    session: requests.Session,
    sleep_seconds: float,
    limit: int,
    sleeper=time.sleep,
    fetch_fn=fetch_post_content,
) -> tuple[dict[str, str], list[str]]:
    """post_id -> 本文。取得失敗した post_id は別リストで返す。"""
    ordered = sorted(post_ids, key=int)
    if limit and limit > 0:
        ordered = ordered[:limit]
    contents: dict[str, str] = {}
    failed: list[str] = []
    for i, pid in enumerate(ordered):
        content = fetch_fn(int(pid), wp_base_url, session, sleeper=sleeper)
        if content is None:
            failed.append(pid)
        else:
            contents[pid] = content
        if i + 1 < len(ordered) and sleep_seconds > 0:
            sleeper(sleep_seconds)
    return contents, failed


# --------------------------------------------------------------------------
# 測定・集計
# --------------------------------------------------------------------------

def build_pairs(
    asin_post_ids: dict[str, list[str]], contents: dict[str, str]
) -> list[dict[str, Any]]:
    """(asin, post_id) ごとにリンク出現位置を持つペアのリスト (本文取得済みのものだけ)。"""
    pairs = []
    for asin, pids in asin_post_ids.items():
        for pid in pids:
            content = contents.get(pid)
            if content is None:
                continue
            spans = find_asin_link_spans(content, asin)
            pairs.append({"asin": asin, "post_id": pid, "content": content, "spans": spans})
    return pairs


def score_pairs(pairs: list[dict[str, Any]], window_chars: int) -> list[dict[str, Any]]:
    """ペアごとに指定 window_chars でのスコアを計算する。"""
    scored = []
    for p in pairs:
        content = p["content"]
        n = len(content)
        counts = []
        for start, end in p["spans"]:
            lo = max(0, start - window_chars)
            hi = min(n, end + window_chars)
            counts.append(count_markers_in_text(strip_html(content[lo:hi])))
        scored.append({
            "asin": p["asin"],
            "post_id": p["post_id"],
            "occurrences": len(counts),
            "max": max(counts) if counts else 0,
            "sum": sum(counts) if counts else 0,
        })
    return scored


def aggregate_by_asin(scored_pairs: list[dict[str, Any]], asins: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """asin -> 窓ごとの最大値の最大 / 窓ごとの合計値の合計 (複数 post にまたがる場合はここで畳む)。"""
    out: dict[str, dict[str, Any]] = {}
    for asin in asins:
        rows = [p for p in scored_pairs if p["asin"] == asin]
        maxes = [r["max"] for r in rows]
        sums = [r["sum"] for r in rows]
        out[asin] = {
            "pair_count": len(rows),
            "max": max(maxes) if maxes else None,
            "sum": sum(sums) if sums else None,
        }
    return out


def distribution_stats(values: list[float]) -> dict[str, Any]:
    """中央値・最小・最大・四分位。"""
    if not values:
        return {"n": 0, "min": None, "q1": None, "median": None, "q3": None, "max": None}
    ordered = sorted(values)
    n = len(ordered)
    if n >= 2:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    else:
        q1 = q3 = ordered[0]
    return {
        "n": n,
        "min": ordered[0],
        "q1": q1,
        "median": statistics.median(ordered),
        "q3": q3,
        "max": ordered[-1],
    }


def full_recall_specificity(pos_values: list[float], neg_values: list[float]) -> dict[str, Any] | None:
    """正例を全部残す最大の閾値 (= min(正例)) のとき、負例を何件落とせるか。"""
    if not pos_values:
        return None
    threshold = min(pos_values)
    dropped = sum(1 for v in neg_values if v < threshold)
    return {"threshold": threshold, "neg_dropped": dropped, "neg_total": len(neg_values)}


def full_drop_specificity(pos_values: list[float], neg_values: list[float]) -> dict[str, Any] | None:
    """負例を全部落とす閾値 (> max(負例)) のとき、正例が何件残るか。"""
    if not neg_values:
        return None
    ceiling = max(neg_values)
    kept = sum(1 for v in pos_values if v > ceiling)
    return {"neg_ceiling": ceiling, "pos_kept": kept, "pos_total": len(pos_values)}


def build_report(
    scored_by_window: dict[int, list[dict[str, Any]]],
    failed_post_ids: list[str],
    zero_link_pairs: int,
    total_pairs: int,
) -> dict[str, Any]:
    windows_report: dict[str, Any] = {}
    for window_chars, scored_pairs in scored_by_window.items():
        agg = aggregate_by_asin(scored_pairs, POSITIVE_ASINS + NEGATIVE_ASINS)
        by_kind: dict[str, Any] = {}
        for kind in ("max", "sum"):
            pos_values = [agg[a][kind] for a in POSITIVE_ASINS if agg[a][kind] is not None]
            neg_values = [agg[a][kind] for a in NEGATIVE_ASINS if agg[a][kind] is not None]
            by_kind[kind] = {
                "positive": distribution_stats(pos_values),
                "negative": distribution_stats(neg_values),
                "full_recall_specificity": full_recall_specificity(pos_values, neg_values),
                "full_drop_specificity": full_drop_specificity(pos_values, neg_values),
                "positive_missing": len(POSITIVE_ASINS) - len(pos_values),
                "negative_missing": len(NEGATIVE_ASINS) - len(neg_values),
            }
        windows_report[str(window_chars)] = by_kind
    return {
        "generated_at": _now_iso(),
        "positive_asins": list(POSITIVE_ASINS),
        "negative_asins": list(NEGATIVE_ASINS),
        "fetch_failed_posts": failed_post_ids,
        "zero_link_pairs": zero_link_pairs,
        "total_pairs": total_pairs,
        "windows": windows_report,
    }


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(
    *,
    sources_path: pathlib.Path,
    out_path: pathlib.Path,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    window_sizes: tuple[int, ...] = WINDOW_SIZES,
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

    contents, failed_ids = fetch_contents(
        unique_ids, wp_base_url, session, sleep_seconds, limit, sleeper=sleeper, fetch_fn=fetch_fn,
    )
    if failed_ids:
        logger.warning("本文取得に失敗した post: %d 件 %s", len(failed_ids), failed_ids)

    pairs = build_pairs(asin_post_ids, contents)
    zero_link_pairs = sum(1 for p in pairs if not p["spans"])

    scored_by_window = {w: score_pairs(pairs, w) for w in window_sizes}
    report = build_report(scored_by_window, failed_ids, zero_link_pairs, len(pairs))

    write_json(out_path, report)
    logger.info("wrote %s (pairs=%d, zero_link_pairs=%d, fetch_failed=%d)",
                out_path, len(pairs), zero_link_pairs, len(failed_ids))
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=DEFAULT_SOURCES_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    ap.add_argument("--limit", type=int, default=0, help="取得する post 数の上限 (スモーク用, 0=無制限)")
    args = ap.parse_args()
    run(
        sources_path=pathlib.Path(args.sources),
        out_path=pathlib.Path(args.out),
        wp_base_url=args.wp_base_url,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
