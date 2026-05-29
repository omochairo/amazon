"""楽天ランキングの未マッチ item を JAN 経由で新規 Amazon ASIN に解決する。

Issue #810 Phase 1 — keyword × top100 の構造的上限を突破する第一歩。

背景:
  ``fetch_rakuten.py`` は楽天おもちゃランキングの各 item を既存記事 ASIN に
  マッチングするが、**未マッチ** (=まだ記事化されていない売れ筋) は
  ``data/raw/rakuten_ranking.json`` に ``matched_asin: null`` で残る。本スクリプトは
  その未マッチ item から JAN を取り出し、**Creator API searchItems(keyword=JAN)**
  の応答を ``itemInfo.externalIds.eans`` で照合して新規 ASIN を解決する (案A)。

  解決済 ASIN は ``fetch_amazon.py --asin <CSV> --competitors-only`` に渡して
  per_asin スナップショットへ backfill する (ライブの amazon.json プールには触れない)。

🚨 使用 API: **Amazon Creator API (creatorsapi.amazon)**。PA-API 5 ではない。
  - searchItems の ``itemInfo.externalIds`` は valid な resource (fetch_amazon の
    SEARCH_ITEM_RESOURCES に既存。04-validate の dry-run gate #785 が検証済)。
  - [[feedback-omochairo-creators-api-deliveryinfo-trap]]

出力:
  - ``data/raw/ranking_sniper_asins.csv``    : 解決済の新規 ASIN を CSV 1 行で出力
                                               (workflow が fetch_amazon --asin に渡す)
  - ``data/raw/_ranking_resolved_manifest.json`` : 可観測性 (resolved / unresolved /
                                               already-covered の内訳)

環境変数: AMAZON_CREATORS_* (creators_api_client.py 参照)
"""
import os
import re
import sys
import json
import time
import pathlib
import logging
import argparse
import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from fetch_rakuten import _extract_jan_from_text  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resolve_ranking_asins")

# searchItems で JAN→ASIN を引くための最小 resources。externalIds で JAN を
# 照合し、title はログ可読性のため。fetch_amazon.SEARCH_ITEM_RESOURCES と矛盾
# しない部分集合 (dry-run gate が本体の方を検証する)。
RESOLVE_RESOURCES = ["itemInfo.title", "itemInfo.externalIds"]


def _safe_get(obj, *attrs, default=None):
    cur = obj
    for a in attrs:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(a)
    return cur if cur is not None else default


def _eans_of(item: dict) -> list:
    """searchItems 応答 item の itemInfo.externalIds.eans.displayValues を返す。"""
    vals = _safe_get(item, "itemInfo", "externalIds", "eans", "displayValues", default=[])
    return [str(v).strip() for v in vals] if isinstance(vals, list) else []


def resolve_jan_to_asin(api, jan: str, search_index: str = "Toys") -> str:
    """Creator API searchItems(keyword=JAN) で JAN 一致 ASIN を 1 件返す (案A)。

    externalIds.eans に問い合わせ JAN を含む item の ASIN を返す。JAN は世界一意
    なので応答 top10 のどれかが一致すれば確定。一致なしは "" を返す。
    """
    try:
        res = api.search_items(
            keywords=jan, search_index=search_index,
            item_count=10, item_page=1, resources=RESOLVE_RESOURCES,
        )
    except Exception as e:
        logger.warning(f"  searchItems failed for JAN {jan}: {e}")
        return ""
    items = _safe_get(res, "searchResult", "items", default=[]) or []
    for it in items:
        if jan in _eans_of(it):
            asin = (it.get("asin") or "").strip()
            if asin:
                return asin
    return ""


def _collect_unmatched_jans(ranking_items: list) -> list:
    """未マッチ ranking item から (jan, rank, title) を rank 順・JAN 重複排除で抽出。"""
    seen = set()
    out = []
    for it in ranking_items:
        if it.get("matched_asin"):
            continue
        text = (it.get("itemCaption") or "") + " " + (it.get("title") or "")
        jan = _extract_jan_from_text(text)
        if jan and jan not in seen:
            seen.add(jan)
            out.append((jan, it.get("rank"), it.get("title")))
    return out


# 記事スラッグ末尾の 10 文字 ASIN サフィックス。fetch_amazon._load_existing_article_asins
# と同一規約 (B0... / ISBN-10 数字 ASIN 両対応、.enrichment/.seo/.quality は除外)。
_ASIN_SUFFIX_RE = re.compile(r"-([A-Z0-9]{10})$")


def _load_covered_asins(articles_dir: str, per_asin_root: str) -> set:
    """既に記事化 / per_asin に存在する ASIN 集合 (再フェッチ不要の判定用)。"""
    covered = set()
    adir = pathlib.Path(articles_dir)
    if adir.is_dir():
        for p in adir.glob("*.json"):
            stem = p.stem
            if stem.endswith((".enrichment", ".seo", ".quality")):
                continue
            m = _ASIN_SUFFIX_RE.search(stem)
            if m:
                covered.add(m.group(1))
    proot = pathlib.Path(per_asin_root)
    if proot.is_dir():
        for d in proot.iterdir():
            if d.is_dir():
                covered.add(d.name.upper())
    return covered


def resolve_ranking_asins(ranking_items, api, covered, limit=0, search_index="Toys", sleep=1.1):
    """純粋ロジック: 未マッチ JAN を解決し manifest dict を返す (テスト driver)。"""
    jans = _collect_unmatched_jans(ranking_items)
    if limit and limit > 0:
        jans = jans[:limit]
    resolved, unresolved, skipped = [], [], []
    seen_asins = set()
    for i, (jan, rank, title) in enumerate(jans):
        asin = resolve_jan_to_asin(api, jan, search_index=search_index)
        if not asin:
            unresolved.append({"jan": jan, "rank": rank, "title": title})
        elif asin.upper() in covered:
            skipped.append({"jan": jan, "asin": asin, "rank": rank})
        elif asin in seen_asins:
            pass  # 別 JAN が同 ASIN に解決 — 重複は無視
        else:
            seen_asins.add(asin)
            resolved.append({"jan": jan, "asin": asin, "rank": rank, "title": title})
        if sleep and i < len(jans) - 1:
            time.sleep(sleep)
    return {
        "input_unmatched_jans": len(jans),
        "resolved": resolved,
        "unresolved": unresolved,
        "skipped_already_covered": skipped,
        "new_asins": [r["asin"] for r in resolved],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", default="data/raw/rakuten_ranking.json")
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--articles-dir", default="data/articles")
    parser.add_argument("--per-asin-root", default="data/raw/per_asin")
    parser.add_argument("--search-index", default="Toys")
    parser.add_argument("--limit", type=int, default=0,
                        help="Resolve at most N unmatched JANs (0 = all). Use --limit 1 for a single-JAN dry-run verification of Option A.")
    parser.add_argument("--sleep", type=float, default=1.1,
                        help="Seconds between searchItems calls (Creator API TPS safety).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and log results but write no output files. For Actions verification of Option A.")
    args = parser.parse_args()

    in_path = pathlib.Path(args.in_path)
    if not in_path.exists():
        logger.error(f"Ranking input not found: {in_path}")
        sys.exit(1)
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    ranking_items = payload.get("items", []) if isinstance(payload, dict) else []

    try:
        from creators_api_client import CreatorsAPIClient
    except ImportError as e:
        logger.error(f"creators_api_client import failed: {e}")
        sys.exit(1)
    api = CreatorsAPIClient()

    covered = _load_covered_asins(args.articles_dir, args.per_asin_root)
    logger.info(f"Covered ASINs (articles ∪ per_asin): {len(covered)}")

    manifest = resolve_ranking_asins(
        ranking_items, api, covered,
        limit=args.limit, search_index=args.search_index, sleep=args.sleep,
    )
    manifest["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    logger.info(
        f"Resolve: unmatched_jans={manifest['input_unmatched_jans']} "
        f"resolved={len(manifest['resolved'])} unresolved={len(manifest['unresolved'])} "
        f"skipped_covered={len(manifest['skipped_already_covered'])}"
    )
    for r in manifest["resolved"]:
        logger.info(f"  resolved JAN {r['jan']} → {r['asin']} (rank={r['rank']})")

    if args.dry_run:
        logger.info("--dry-run: no output files written.")
        print(",".join(manifest["new_asins"]))
        return

    os.makedirs(args.out, exist_ok=True)
    (pathlib.Path(args.out) / "ranking_sniper_asins.csv").write_text(
        ",".join(manifest["new_asins"]), encoding="utf-8"
    )
    (pathlib.Path(args.out) / "_ranking_resolved_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(",".join(manifest["new_asins"]))


if __name__ == "__main__":
    main()
