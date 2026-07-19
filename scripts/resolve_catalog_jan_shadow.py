"""楽天カタログ (商品価格ナビ Product/Search) 経由の exact JAN 解決 — shadow レーン。

Issue #3561 (2026-07-19 設計コメントで確定):
  title-fuzzy は「楽天リスティング → Amazon」を直接あいまい照合しており、ノイズの
  多い2カタログ同士の突き合わせで誤マッチ率が高い。本スクリプトは間に「楽天カタログ」
  を挟んだ2段経路を検証する:

    ランキング item (listing)
      → [Stage R] Product/Search でカタログ候補 (≤5件)
      → ガードレール G1-G4 で候補検証
      → [Stage A] productCode (JAN) を既存 resolve_jan_to_item() で exact 解決
      → 既存 #3551 genre gate + 価格±40% を Amazon item に適用

  クエリ生成は **リコール確保の手段でしかなく、採否の正しさはガードレールが保証する**
  (fail-closed)。title-fuzzy 3点ガード (brand / 型番トークン / 価格±40%) と同じ
  precision 優先・recall 犠牲の方針 (#2818 2026-07-11 確定) を踏襲し、G4 (ISBN 除外)
  を新設する (実測の「ベイブレード→雑誌ISBN」事故を構造的に封じる)。

  **shadow = 書き込みなし**。manifest artifact のみを出力し、本線データ
  (``_ranking_resolved_manifest.json`` / per_asin / ranking_pool 等) には一切触れない。
  較正 (全件手動 spot check・誤マッチ0確認) が済むまで ``resolve_ranking_asins.py``
  への統合は行わない (issue #3561 進め方 2-3)。

エンドポイント: ``https://openapi.rakuten.co.jp/ichibaproduct/api/Product/Search/20250801``
  - 認証は既存 secrets (RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY) でそのまま通る。
  - accessKey は params 側。Referer / Origin ヘッダが必須 (fetch_rakuten.py と同一パターン)。
  - 1 req/sec でも 429 実測。3秒/req + 429 backoff で回す。

対象母集団: 直近ランキング (``data/raw/rakuten_ranking.json``) の
  「JAN 無し・title-fuzzy 未解決」の残党のみ (件数小)。JAN が抽出できる item は
  既存の JAN 直接解決経路が優先的に処理するため対象外。

環境変数: RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY / AMAZON_CREATORS_* (creators_api_client.py 参照)
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

import fetch_rakuten as rakuten  # noqa: E402
import resolve_ranking_asins as rr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resolve_catalog_jan_shadow")

RAKUTEN_PRODUCT_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibaproduct/api/Product/Search/20250801"

# 楽天 Product/Search は 1 req/sec でも 429 実測 (issue #3561 spike)。fetch_rakuten の
# RAKUTEN_API_GAP_SEC (1.5s、Search/Ranking 用) より保守的に 3 秒/req を既定にする。
RAKUTEN_API_GAP_SEC = 3.0

# Stage R クエリの目標文字数上限 (issue #3561 設計: 「32文字程度に詰める」)。
QUERY_MAX_LEN = 32

# G4: productCode が ISBN (978/979 始まり13桁) は無条件 reject。
_ISBN_RE = re.compile(r"^(978|979)\d{10}$")


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Stage R: クエリ生成 (識別力順: 型番トークン → ブランド語 → タイトル先頭トークン)
# --------------------------------------------------------------------------

def _ordered_model_tokens(text: str) -> list:
    """型番トークンをタイトル出現順・重複排除で返す (`_extract_model_tokens` の順序保持版)。"""
    seen, out = set(), []
    for m in rr._MODEL_TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        if not rr._HAS_DIGIT_RE.search(tok):
            continue
        up = tok.upper()
        if up in seen:
            continue
        seen.add(up)
        out.append(tok)
    return out


def _fit_tokens(tokens: list, max_len: int = QUERY_MAX_LEN) -> str:
    """トークンを空白区切りで連結しつつ max_len 文字程度に収める。

    最初のトークン自体が max_len を超える場合はそのトークンを切り詰めて返す
    (400 keyword parameter value is not valid を避けるための保守側)。
    """
    out, cur_len = [], 0
    for t in tokens:
        if not t:
            continue
        add_len = len(t) + (1 if out else 0)
        if cur_len + add_len > max_len:
            if not out:
                return t[:max_len]
            break
        out.append(t)
        cur_len += add_len
    return " ".join(out)


def build_catalog_queries(title: str) -> list:
    """Stage R クエリを (stage_label, query) のリストで返す (primary + リトライ最大2段)。

    primary: 型番トークン + ブランド語 + タイトル先頭トークンを識別力順に 32字詰め。
    retry_model: 型番トークンのみ。
    retry_brand: ブランド語 + タイトル先頭2トークン。
    空・重複になる段はスキップする (issue #3561 設計: リトライは2段まで)。
    """
    normalized = rr._normalize_title_for_search(title)
    if not normalized:
        return []

    model_tokens = _ordered_model_tokens(normalized)
    brand = rr._brand_of(normalized)
    leading_tokens = normalized.split()

    primary_tokens = list(model_tokens)
    if brand and brand not in primary_tokens:
        primary_tokens.append(brand)
    for t in leading_tokens:
        if t not in primary_tokens:
            primary_tokens.append(t)
    primary_query = _fit_tokens(primary_tokens)

    queries = []
    seen_queries = set()
    if primary_query:
        queries.append(("primary", primary_query))
        seen_queries.add(primary_query)

    if model_tokens:
        retry_model_query = _fit_tokens(model_tokens)
        if retry_model_query and retry_model_query not in seen_queries:
            queries.append(("retry_model", retry_model_query))
            seen_queries.add(retry_model_query)

    retry_brand_tokens = ([brand] if brand else []) + leading_tokens[:2]
    retry_brand_query = _fit_tokens(retry_brand_tokens)
    if retry_brand_query and retry_brand_query not in seen_queries:
        queries.append(("retry_brand", retry_brand_query))
        seen_queries.add(retry_brand_query)

    return queries


# --------------------------------------------------------------------------
# Stage R: Product/Search 呼び出し
# --------------------------------------------------------------------------

def _extract_product_fields(raw: dict) -> dict:
    p = raw.get("Product", raw) if isinstance(raw, dict) else {}
    return {
        "productName": p.get("productName", "") or "",
        "productNo": (p.get("productNo", "") or "").strip(),
        "makerCode": p.get("makerCode", "") or "",
        "makerName": p.get("makerName", "") or "",
        "productCode": (p.get("productCode", "") or "").strip(),
    }


def fetch_catalog_candidates(app_id: str, access_key: str, query: str, hits: int = 5,
                             max_retries: int = 1, backoff_sec: float = 3.0) -> list:
    """Product/Search を叩きカタログ候補 (≤hits 件) を返す。失敗時は空リスト。"""
    headers = {
        "Referer": "https://github.com/omochairo/amazon",
        "Origin": "https://github.com/omochairo/amazon",
    }
    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": query,
        "hits": hits,
    }
    resp = rakuten._rakuten_get_with_retry(
        RAKUTEN_PRODUCT_SEARCH_URL, params, headers, label="Product/Search",
        max_retries=max_retries, backoff_sec=backoff_sec,
    )
    if resp is None or resp.status_code != 200:
        if resp is not None:
            logger.warning(f"  Product/Search failed for {query!r}: HTTP {resp.status_code} {resp.text[:200]}")
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    products = data.get("Products", []) if isinstance(data, dict) else []
    if not isinstance(products, list):
        return []
    return [_extract_product_fields(p) for p in products[:hits]]


# --------------------------------------------------------------------------
# ガードレール G1-G4
# --------------------------------------------------------------------------

def _is_isbn(product_code: str) -> bool:
    """G4: productCode が 978/979 始まり13桁 (ISBN) なら True。"""
    return bool(_ISBN_RE.match((product_code or "").strip()))


def evaluate_catalog_candidate(listing_title: str, product: dict) -> dict:
    """カタログ候補 1 件を G1/G2/G4 で評価する (G3 は Stage A 後に別途適用)。"""
    product_name = product.get("productName") or ""
    product_no = (product.get("productNo") or "").strip()
    maker_code = product.get("makerCode") or ""
    maker_name = product.get("makerName") or ""
    product_code = (product.get("productCode") or "").strip()

    # G1: brand 一致必須。双方 unknown は不成立 (title-fuzzy の _brand_of と同方針)。
    listing_brand = rr._brand_of(listing_title)
    candidate_brand = rr._brand_of(product_name) or rr._brand_of(maker_name) or rr._brand_of(maker_code)
    g1_brand_match = bool(listing_brand) and listing_brand == candidate_brand

    # G2: productNo 優先の型番照合。欠損時のみ productName とのトークン共通照合。
    listing_tokens = rr._extract_model_tokens(listing_title)
    if product_no:
        g2_model_match = product_no.upper() in listing_tokens
        g2_method = "productNo"
    else:
        candidate_tokens = rr._extract_model_tokens(product_name)
        g2_model_match = bool(listing_tokens & candidate_tokens)
        g2_method = "token_fallback"

    g4_is_isbn = _is_isbn(product_code)

    return {
        "productName": product_name,
        "productNo": product_no,
        "makerCode": maker_code,
        "makerName": maker_name,
        "productCode": product_code,
        "g1_brand_match": g1_brand_match,
        "g1_listing_brand": listing_brand,
        "g1_candidate_brand": candidate_brand,
        "g2_model_match": g2_model_match,
        "g2_method": g2_method,
        "g4_is_isbn": g4_is_isbn,
        "passed_stage_r": g1_brand_match and g2_model_match and not g4_is_isbn,
    }


def _price_within_tolerance(rakuten_price: float, amazon_price: float, tolerance: float = 0.4):
    """G3: 楽天リスティング価格と Amazon item 価格の ±tolerance 判定。片方欠損は None (indeterminate)。"""
    if not rakuten_price or not amazon_price:
        return None
    lo = rakuten_price * (1 - tolerance)
    hi = rakuten_price * (1 + tolerance)
    return lo <= amazon_price <= hi


# --------------------------------------------------------------------------
# 対象母集団抽出: JAN 無し・title-fuzzy 未解決の残党
# --------------------------------------------------------------------------

def collect_shadow_population(ranking_items: list, manifest: dict = None) -> list:
    """`_ranking_resolved_manifest.json` を参照し、JAN 無し・title-fuzzy 未解決の残党を返す。

    JAN 抽出可能 / 記事化済み等は `resolve_ranking_asins._collect_unmatched_no_jan` が
    既に除外する。title_fuzzy が有効化されて manifest に結果がある場合は、そちらで
    既に解決済 (rank) の item をさらに除外する (二重処理防止)。
    """
    population = rr._collect_unmatched_no_jan(ranking_items)
    if manifest:
        tf = manifest.get("title_fuzzy") or {}
        resolved_ranks = {
            e.get("rank") for e in (tf.get("title_fuzzy_resolved") or [])
            if e.get("rank") is not None
        }
        if resolved_ranks:
            population = [it for it in population if it.get("rank") not in resolved_ranks]
    return population


# --------------------------------------------------------------------------
# per-item 処理 (Stage R → ガードレール → Stage A → G3/ジャンルゲート)
# --------------------------------------------------------------------------

def process_item(item: dict, app_id: str, access_key: str, api, hits: int = 5,
                 sleep: float = RAKUTEN_API_GAP_SEC, enable_genre_gate: bool = True) -> dict:
    title = item.get("title") or ""
    rank = item.get("rank")
    rakuten_price = _to_float(item.get("price"))

    queries = build_catalog_queries(title)
    query_attempts = []
    candidates_raw = []
    for i, (stage, query) in enumerate(queries):
        if i > 0 and sleep:
            time.sleep(sleep)
        prods = fetch_catalog_candidates(app_id, access_key, query, hits=hits)
        query_attempts.append({"stage": stage, "query": query, "candidate_count": len(prods)})
        if prods:
            candidates_raw = prods
            break

    candidates_eval = []
    selected = None
    for p in candidates_raw:
        ev = evaluate_catalog_candidate(title, p)
        candidates_eval.append(ev)
        if selected is None and ev["passed_stage_r"]:
            selected = (p, ev)

    record = {
        "rank": rank,
        "title": title,
        "rakuten_price": rakuten_price,
        "query_attempts": query_attempts,
        "candidates": candidates_eval,
        "selected_candidate": None,
        "stage_a": None,
        "accepted": False,
    }

    if selected is None:
        return record

    product, ev = selected
    record["selected_candidate"] = ev
    jan = ev["productCode"]
    if not jan:
        record["stage_a"] = {"attempted": False, "reason": "no_product_code"}
        return record

    if sleep:
        time.sleep(sleep)
    amazon_item = rr.resolve_jan_to_item(api, jan)
    asin = (amazon_item.get("asin") or "").strip()
    if not asin:
        record["stage_a"] = {
            "attempted": True, "jan": jan, "asin": "",
            "accepted": False, "reason": "jan_not_found_on_amazon",
        }
        return record

    amazon_price = rr._candidate_price(amazon_item)
    price_ok = _price_within_tolerance(rakuten_price, amazon_price)

    genre_verdict, genre_roots = ("indeterminate", [])
    if enable_genre_gate:
        genre_verdict, genre_roots = rr._genre_verdict_for_item(amazon_item)
    genre_rejected = enable_genre_gate and rr._is_genre_rejected(genre_verdict)

    accepted = bool(price_ok) and not genre_rejected

    stage_a = {
        "attempted": True,
        "jan": jan,
        "asin": asin,
        "amazon_price": amazon_price,
        "price_ok": price_ok,
        "genre_verdict": genre_verdict,
        "genre_roots": genre_roots,
        "accepted": accepted,
    }
    if not accepted:
        stage_a["reason"] = "genre_gate" if genre_rejected else "price_out_of_range"
    record["stage_a"] = stage_a
    record["accepted"] = accepted
    return record


def run_shadow(items: list, app_id: str, access_key: str, api, limit: int = 0, hits: int = 5,
              sleep: float = RAKUTEN_API_GAP_SEC, enable_genre_gate: bool = True) -> dict:
    """純粋ロジック: shadow population を処理し manifest dict を返す (テスト driver)。"""
    candidates_before_limit = len(items)
    if limit and limit > 0:
        items = items[:limit]

    records = []
    for i, item in enumerate(items):
        records.append(process_item(
            item, app_id, access_key, api, hits=hits, sleep=sleep,
            enable_genre_gate=enable_genre_gate,
        ))
        if sleep and i < len(items) - 1:
            time.sleep(sleep)

    accepted = [
        {"rank": r["rank"], "title": r["title"], "jan": r["stage_a"]["jan"], "asin": r["stage_a"]["asin"]}
        for r in records if r.get("accepted")
    ]

    return {
        "shadow": True,
        "genre_gate_enabled": bool(enable_genre_gate),
        "input_population_before_limit": candidates_before_limit,
        "input_population": len(items),
        "items": records,
        "accepted_count": len(accepted),
        "accepted": accepted,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ranking-in", default="data/raw/rakuten_ranking.json")
    parser.add_argument("--manifest-in", default="data/raw/_ranking_resolved_manifest.json",
                        help="Committed resolve manifest, used only to exclude items already "
                             "resolved by title_fuzzy (if that section is present). Not modified.")
    parser.add_argument("--out", default="data/raw/_catalog_jan_shadow_manifest.json",
                        help="Shadow-only manifest output. No other file is written.")
    parser.add_argument("--limit", type=int, default=0, help="Max shadow-population items to process (0 = all).")
    parser.add_argument("--hits", type=int, default=5, help="Product/Search candidates per query (<=5).")
    parser.add_argument("--sleep", type=float, default=RAKUTEN_API_GAP_SEC,
                        help="Seconds between Rakuten API calls (issue #3561 spike: 3s/req + 429 backoff).")
    parser.add_argument("--no-genre-gate", dest="genre_gate", action="store_false",
                        help="Disable #2823 genre gate on Stage A results (emergency escape hatch; default on).")
    args = parser.parse_args()

    ranking_path = pathlib.Path(args.ranking_in)
    if not ranking_path.exists():
        logger.error(f"Ranking input not found: {ranking_path}")
        sys.exit(1)
    payload = json.loads(ranking_path.read_text(encoding="utf-8"))
    ranking_items = payload.get("items", []) if isinstance(payload, dict) else []

    manifest_resolved = None
    manifest_path = pathlib.Path(args.manifest_in)
    if manifest_path.exists():
        try:
            manifest_resolved = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load {manifest_path}: {e}")

    population = collect_shadow_population(ranking_items, manifest_resolved)
    logger.info(f"Shadow population (no-JAN, title-fuzzy unresolved): {len(population)}")

    app_id = os.environ.get("RAKUTEN_APP_ID")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY")
    if not app_id or not access_key:
        logger.error("RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY not set")
        sys.exit(1)

    try:
        from creators_api_client import CreatorsAPIClient
    except ImportError as e:
        logger.error(f"creators_api_client import failed: {e}")
        sys.exit(1)
    api = CreatorsAPIClient()

    manifest = run_shadow(
        population, app_id, access_key, api,
        limit=args.limit, hits=args.hits, sleep=args.sleep,
        enable_genre_gate=args.genre_gate,
    )
    manifest["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    logger.info(
        f"Shadow result: population={manifest['input_population']} "
        f"(before_limit={manifest['input_population_before_limit']}) "
        f"accepted={manifest['accepted_count']}"
    )
    for a in manifest["accepted"]:
        logger.info(f"  accepted rank={a['rank']} jan={a['jan']} -> asin={a['asin']} title={a['title']!r}")

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Wrote shadow manifest to {out_path}")


if __name__ == "__main__":
    main()
