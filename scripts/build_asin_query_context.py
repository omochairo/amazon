"""build_asin_query_context.py

#2953 B案 (#1329 の記事版): 蓄積された GSC 週次スナップショット
(data/analytics/gsc_history/<YYYY-Www>.json, by_combo 込み) を読み、指定 ASIN の
商品ページ (/products/<asin>/) に実際に流入している検索クエリ群を出力する
read-only スクリプト。

scripts/build_brand_query_context.py (#1329 Phase 3A) の設計 (閾値・正規化・
no-inband-IO の慣行 [[feedback-omochairo-build-post-no-inband-io]]) を踏襲するが、
対象がブランド名の全文一致ではなく特定 ASIN の商品ページ着地クエリである点が異なる。

マッチング: by_combo の page を URL decode + lowercase したパスが
`/products/<asin.lower()>/` (末尾スラッシュ有無は許容) と一致する行のみ採用。

集計: query 正規化 (NFKC + lowercase + 括弧 strip + 空白圧縮) をキーに週横断で合算。
impressions_sum・clicks_sum は単純合算、position は impressions 加重平均、
weeks_seen はそのクエリが観測された週ファイル数。代表表記 (query) は最も
impressions が多かった週の生表記を採用する。

採用閾値 (brand 版と同一): 直近 N 週合算 impressions_sum>=min_impressions_sum、
または weeks_seen>=2 かつ impressions_sum>=1 (単週 GSC は impressions 1-9 が大半)。
impressions_sum 降順で max_queries 件まで。

--weeks は「存在するスナップショットファイルの新しい方から N 件」であり、暦週換算では
ない (GSC 週次スナップショットは欠番が生じるため)。

queries が 0 件なら fallback:true を返し、呼び出し側 (workflow / build_jules_prompt.py)
で「GSC context 無し」分岐させる。

呼び出し元:
- .github/workflows/03-invoke-jules.yml (新規記事生成 + 12-rewrite 経由のリライト、
  いずれも wf03 が処理するため注入箇所は一箇所で足りる)
- scripts/build_jules_prompt.py (GitLab 予備系パリティ)

Issue: https://github.com/omochairo/amazon/issues/2953, https://github.com/omochairo/amazon/issues/1329
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_asin_query_context")

DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_WEEKS = 4
DEFAULT_MIN_IMPRESSIONS_SUM = 3
DEFAULT_MAX_QUERIES = 8

_BRACKET_RE = re.compile(r"[()\[\]{}「」『』【】（）]")
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """NFKC + lowercase + 括弧除去 + 空白畳み。query 比較の共通キー (brand 版と同一仕様)。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _BRACKET_RE.sub(" ", t)
    return _SPACE_RE.sub(" ", t).strip()


def normalize_page_path(url: str) -> str:
    """page URL を decode + path 抽出 + NFKC lowercase。"""
    if not url:
        return ""
    decoded = urllib.parse.unquote(str(url))
    path = urllib.parse.urlparse(decoded).path or decoded
    return unicodedata.normalize("NFKC", path).lower()


def _asin_page_match(norm_path: str, asin_norm: str) -> bool:
    """path が /products/<asin>/ (末尾スラッシュ有無は許容) と一致するか。"""
    segs = [s for s in norm_path.split("/") if s]
    return len(segs) == 2 and segs[0] == "products" and segs[1] == asin_norm


def load_snapshots(history_dir: pathlib.Path, weeks: int) -> list[tuple[str, dict[str, Any]]]:
    """gsc_history/*.json を週ラベル昇順で読み、直近 weeks 件 (ファイル数ベース) を返す。
    欠番週があっても「存在するファイルの新しい方から N 件」として扱う。
    壊れたファイルは warning を出して読み飛ばし、他ファイルの処理は継続する。"""
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


def _aggregate(snapshots: list[tuple[str, dict[str, Any]]], asin_norm: str) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for week_label, gsc in snapshots:
        for row in gsc.get("by_combo") or []:
            norm_path = normalize_page_path(row.get("page", ""))
            if not _asin_page_match(norm_path, asin_norm):
                continue
            q_raw = row.get("query", "")
            if not q_raw:
                continue
            key = normalize(q_raw)
            if not key:
                continue
            imp = row.get("impressions", 0) or 0
            clk = row.get("clicks", 0) or 0
            pos = row.get("position", 0) or 0

            a = agg.setdefault(key, {
                "impressions_sum": 0,
                "clicks_sum": 0,
                "weeks_seen": set(),
                "weighted_position_sum": 0.0,
                "best_impressions": -1,
                "best_query_raw": q_raw,
            })
            a["impressions_sum"] += imp
            a["clicks_sum"] += clk
            a["weeks_seen"].add(week_label)
            a["weighted_position_sum"] += pos * imp
            if imp > a["best_impressions"]:
                a["best_impressions"] = imp
                a["best_query_raw"] = q_raw
    return agg


def build_context(asin: str, *,
                   history_dir: str = DEFAULT_HISTORY_DIR,
                   weeks: int = DEFAULT_WEEKS,
                   max_queries: int = DEFAULT_MAX_QUERIES,
                   min_impressions_sum: int = DEFAULT_MIN_IMPRESSIONS_SUM) -> dict[str, Any]:
    """指定 ASIN の商品ページに実流入している検索クエリを集計して返す。read-only。"""
    snapshots = load_snapshots(pathlib.Path(history_dir), weeks)
    asin_norm = (asin or "").strip().lower()
    agg = _aggregate(snapshots, asin_norm)

    queries = []
    for a in agg.values():
        impressions_sum = a["impressions_sum"]
        weeks_seen = len(a["weeks_seen"])
        keep = (impressions_sum >= min_impressions_sum
                or (weeks_seen >= 2 and impressions_sum >= 1))
        if not keep:
            continue
        position = (a["weighted_position_sum"] / impressions_sum) if impressions_sum > 0 else 0.0
        queries.append({
            "query": a["best_query_raw"],
            "impressions": impressions_sum,
            "clicks": a["clicks_sum"],
            "position": round(position, 1),
            "weeks_seen": weeks_seen,
        })
    queries.sort(key=lambda r: (r["impressions"], r["weeks_seen"]), reverse=True)
    queries = queries[:max_queries]

    return {
        "asin": (asin or "").strip().upper(),
        "weeks_scanned": len(snapshots),
        "queries": queries,
        "fallback": len(queries) == 0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asin", required=True, help="対象 ASIN (例: B0XXXXXXXX)")
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help="存在するスナップショットの新しい方から何ファイル分を読むか (0 で全件)")
    p.add_argument("--max-queries", type=int, default=DEFAULT_MAX_QUERIES)
    p.add_argument("--min-impressions-sum", type=int, default=DEFAULT_MIN_IMPRESSIONS_SUM)
    args = p.parse_args()

    result = build_context(
        args.asin,
        history_dir=args.history_dir,
        weeks=args.weeks,
        max_queries=args.max_queries,
        min_impressions_sum=args.min_impressions_sum,
    )
    logger.info("asin=%s weeks_scanned=%d queries=%d fallback=%s",
                result["asin"], result["weeks_scanned"], len(result["queries"]), result["fallback"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
