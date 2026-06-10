"""build_brand_query_context.py

Phase 3A (#1329): 蓄積された GSC 週次スナップショット
(data/analytics/gsc_history/<YYYY-Www>.json, by_combo 込み) を読み、指定ブランドに
紐づく検索クエリ群と、全 brand 共通の SEO ヒントを出力する read-only スクリプト。

14-brand-narrative.yml の prompt builder から `--brand <canonical>` で呼ばれ、出力 JSON を
Jules プロンプトの「読者が実際に検索している語」セクションに注入する想定 (プロンプト注入
自体は Phase 3B / 別 PR)。本スクリプトは描画・外部 IO を持たず、事前蓄積済みデータを読む
だけ ([[feedback-omochairo-build-post-no-inband-io]])。

マッチング (session 77 設計 + comment 2 補正):
1. ブランド名 substring match: 正規化クエリが brand canonical/alias を含む (信頼度最高)
2. page_path match: by_combo の page が /brands/<brand>/... 配下 → その query を採用 (補助)
正規化: NFKC + lowercase + 括弧 strip。page は URL decode + lowercase (ASIN 双方向対応)。

採用閾値: 直近 N 週合算 impressions_sum>=3、または weeks_seen>=2 かつ impressions_sum>=1
(単週 GSC は impressions 1-9 が大半のため)。最大 8 クエリ。

global_seo_hints: brand 非依存で、全スナップショットの query 全体から年齢系 (中学年/N歳 等) と
購入意図系 (ランキング/比較/レビュー 等) のキーワードを抽出。全 brand 共通コンテキスト。

queries が 0 件なら fallback:true を返し、workflow 側で「GSC context 無し」分岐させる。

Issue: https://github.com/omochairo/amazon/issues/1329
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
import unicodedata
import urllib.parse
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_brand_query_context")

DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_TAXONOMY = "data/brand_taxonomy.yaml"
DEFAULT_WEEKS = 4
DEFAULT_MIN_IMPRESSIONS_SUM = 3
DEFAULT_MAX_QUERIES = 8

# global_seo_hints 用キーワード (brand 非依存)
_AGE_RE = re.compile(r"(\d+\s*(?:歳|才)|低学年|中学年|高学年|未就学|幼児|小学生|園児)")
INTENT_KEYWORDS = [
    "ランキング", "比較", "レビュー", "おすすめ", "オススメ", "口コミ",
    "評価", "評判", "人気", "選び方", "最安", "安い", "コスパ",
]

_BRACKET_RE = re.compile(r"[()\[\]{}「」『』【】（）]")
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """NFKC + lowercase + 括弧除去 + 空白畳み。brand 名・query 比較の共通キー。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _BRACKET_RE.sub(" ", t)
    return _SPACE_RE.sub(" ", t).strip()


def normalize_page_path(url: str) -> str:
    """page URL を decode + path 抽出 + NFKC lowercase。/products/ASIN 双方向対応。"""
    if not url:
        return ""
    decoded = urllib.parse.unquote(str(url))
    path = urllib.parse.urlparse(decoded).path or decoded
    return unicodedata.normalize("NFKC", path).lower()


def load_brand_terms(taxonomy_path: pathlib.Path, brand: str) -> tuple[str, set[str]]:
    """指定 brand の canonical+aliases を正規化 term set で返す。
    taxonomy 未登録 / ファイル無しなら brand 文字列のみ。"""
    canonical = brand
    terms = {normalize(brand)}
    try:
        data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return canonical, {t for t in terms if t}
    nb = normalize(brand)
    for b in data.get("brands", []) or []:
        names = [b.get("canonical")] + list(b.get("aliases") or [])
        norm_names = {normalize(n) for n in names if n}
        if nb in norm_names:
            canonical = b.get("canonical") or brand
            terms |= norm_names
            break
    return canonical, {t for t in terms if t}


def load_snapshots(history_dir: pathlib.Path, weeks: int) -> list[tuple[str, dict[str, Any]]]:
    """gsc_history/*.json を週ラベル昇順で読み、直近 weeks 件を返す。
    ファイル名 (例 2026-W23) は ISO 週で辞書順 == 時系列順。"""
    if not history_dir.exists():
        return []
    files = sorted(p for p in history_dir.glob("*.json") if p.is_file())
    if weeks > 0:
        files = files[-weeks:]
    out: list[tuple[str, dict[str, Any]]] = []
    for p in files:
        try:
            out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
        except (ValueError, OSError) as exc:
            logger.warning("skip unreadable snapshot %s: %s", p, exc)
    return out


def _query_matches_brand(norm_query: str, terms: set[str]) -> bool:
    return any(term and term in norm_query for term in terms)


def _brand_page_match(norm_path: str, terms: set[str]) -> bool:
    """/brands/<segment>/... の <segment> が brand term なら True。"""
    segs = [s for s in norm_path.split("/") if s]
    return len(segs) >= 2 and segs[0] == "brands" and segs[1] in terms


def build_context(snapshots: list[tuple[str, dict[str, Any]]], canonical: str,
                  terms: set[str], *,
                  min_impressions_sum: int = DEFAULT_MIN_IMPRESSIONS_SUM,
                  max_queries: int = DEFAULT_MAX_QUERIES) -> dict[str, Any]:
    # query -> {impressions_sum, clicks_sum, weeks_seen, sources}
    agg: dict[str, dict[str, Any]] = {}
    pages: set[str] = set()
    weeks_list: list[str] = []

    for week_label, gsc in snapshots:
        weeks_list.append(week_label)
        by_query_map = {r.get("query"): r for r in (gsc.get("by_query") or []) if r.get("query")}
        # per-week candidate queries: {query -> {imp, clk, src}}
        week_q: dict[str, dict[str, Any]] = {}

        # 1. brand-name substring match (by_query は当該クエリの全ページ合算 = 権威値)
        for q, row in by_query_map.items():
            if _query_matches_brand(normalize(q), terms):
                week_q[q] = {"imp": row.get("impressions", 0),
                             "clk": row.get("clicks", 0), "src": {"brand_name"}}

        # 2. page_path match via by_combo (/brands/<brand>/...)
        for row in gsc.get("by_combo") or []:
            np = normalize_page_path(row.get("page", ""))
            if not _brand_page_match(np, terms):
                continue
            pages.add(np)
            q = row.get("query", "")
            if not q:
                continue
            if q in week_q:
                week_q[q]["src"].add("page_path")
            else:
                # by_query 権威値があれば優先、無ければ combo 値 (二重計上回避)
                base = by_query_map.get(q)
                week_q[q] = {
                    "imp": (base or row).get("impressions", 0),
                    "clk": (base or row).get("clicks", 0),
                    "src": {"page_path"},
                }

        # fold week -> agg
        for q, e in week_q.items():
            a = agg.setdefault(q, {"impressions_sum": 0, "clicks_sum": 0,
                                   "weeks_seen": 0, "sources": set()})
            a["impressions_sum"] += e["imp"]
            a["clicks_sum"] += e["clk"]
            a["weeks_seen"] += 1
            a["sources"] |= e["src"]

    queries = []
    for q, a in agg.items():
        keep = (a["impressions_sum"] >= min_impressions_sum
                or (a["weeks_seen"] >= 2 and a["impressions_sum"] >= 1))
        if not keep:
            continue
        queries.append({
            "query": q,
            "impressions_sum": a["impressions_sum"],
            "clicks_sum": a["clicks_sum"],
            "weeks_seen": a["weeks_seen"],
            "match": sorted(a["sources"]),
        })
    queries.sort(key=lambda r: (r["impressions_sum"], r["weeks_seen"]), reverse=True)
    queries = queries[:max_queries]

    return {
        "brand": canonical,
        "weeks_covered": len(snapshots),
        "weeks": weeks_list,
        "queries": queries,
        "page_paths": sorted(pages),
        "fallback": len(queries) == 0,
    }


def global_seo_hints(snapshots: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """全 brand 共通: 全 query から年齢系 / 購入意図系キーワードを抽出。"""
    age: set[str] = set()
    intent: set[str] = set()
    for _, gsc in snapshots:
        for row in gsc.get("by_query") or []:
            q = row.get("query", "") or ""
            for m in _AGE_RE.findall(q):
                age.add(_SPACE_RE.sub("", m))
            for kw in INTENT_KEYWORDS:
                if kw in q:
                    intent.add(kw)
    age_sorted = sorted(age)
    intent_sorted = sorted(intent)
    rationale = ""
    if age_sorted or intent_sorted:
        rationale = (f"navi.omcha.jp の直近 {len(snapshots)} 週で観測された検索意図 "
                     f"(年齢層 {len(age_sorted)} 種 / 購入意図 {len(intent_sorted)} 種)。"
                     "データに無い年齢 floor・事実は捏造しないこと。")
    return {
        "age_keywords_seen": age_sorted,
        "intent_keywords_seen": intent_sorted,
        "rationale": rationale,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brand", required=True, help="canonical ブランド名 (14-brand-narrative の select 結果)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--taxonomy", default=DEFAULT_TAXONOMY)
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS, help="直近何週分を読むか (0 で全件)")
    p.add_argument("--min-impressions-sum", type=int, default=DEFAULT_MIN_IMPRESSIONS_SUM)
    p.add_argument("--max-queries", type=int, default=DEFAULT_MAX_QUERIES)
    p.add_argument("--out", default=None, help="未指定なら stdout に出力")
    args = p.parse_args()

    snapshots = load_snapshots(pathlib.Path(args.history_dir), args.weeks)
    canonical, terms = load_brand_terms(pathlib.Path(args.taxonomy), args.brand)
    logger.info("brand=%s terms=%d snapshots=%d", canonical, len(terms), len(snapshots))

    result = build_context(
        snapshots, canonical, terms,
        min_impressions_sum=args.min_impressions_sum,
        max_queries=args.max_queries,
    )
    result["global_seo_hints"] = global_seo_hints(snapshots)
    result["params"] = {
        "weeks": args.weeks,
        "min_impressions_sum": args.min_impressions_sum,
        "max_queries": args.max_queries,
        "taxonomy_term_count": len(terms),
    }

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        logger.info("wrote %s (%d queries, fallback=%s)", out, len(result["queries"]), result["fallback"])
    else:
        print(payload)
        logger.info("%d queries, fallback=%s", len(result["queries"]), result["fallback"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
