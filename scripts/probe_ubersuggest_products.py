#!/usr/bin/env python3
"""probe_ubersuggest_products.py

Ubersuggest 需要語 (L1 通過分) が Amazon の商品として成立するかを **観測だけ**
する shadow レーン (#2686 PR-D)。

なぜ必要か:
  scripts/ingest_ubersuggest.py (PR-C) の語彙ゲート (data/demand_query_rules.yaml)
  だけでは商品抽出はできない。実測で L1 通過後も上位に非商品が残る
  (「知育 村」「みみっち」「すいちゃん みいつけた」「こども新聞」
  「たまごっち 種類」等)。これらは主題そのものが非商品というより、Amazon の
  商品として検索して初めて分かる種類の失敗 (キャラクター名単体・雑誌名・
  「種類」のような商品を指さない末尾語) なので、実際に SearchItems を叩いて
  判定する必要がある。

処理の流れ (語ごと):
  1. data/analytics/ubersuggest_demand.json の keywords を Volume 降順に
     --limit 件取る
  2. **raw_query** (空白を保持した元表記) で SearchItems を叩く。query は
     build_demand_keywords.normalize_key で重複排除のため空白を除去した
     キーなので検索語にしてはいけない (「保育園シール貼り」という一続きの
     文字列で検索すると「保育園 シール貼り」より一致率が落ちる)。
  3. 返った商品にジャンル判定 (scripts/genre_gate.classify_genre、再実装しない)
     をかけ、"pass" だけを genre_pass_hits として数える。"indeterminate"
     (fail-open) は生成パイプライン (fetch_amazon.py) では素通りさせるが、
     本レーンは「確認できたか」の観測なので indeterminate は数えない
     (fail-open にしない)。
  4. genre_pass_hits の商品タイトルとクエリ語の重なりを見て (下記
     compute_title_overlap)、verdict を確定する。

verdict の判定基準 (2026-08-10 設計、2026-08-10 owner レビューで partial/zero を
入れ替え・docstring 固定・unit test で担保):
  - hits == 0                        → non_product (no_hits)   Amazon に何も無い
  - genre_pass_hits == 0             → non_product (no_genre_pass)
      供給はあるがおもちゃ/ベビー領域ではない
  - coverage == 1.0 (クエリの全トークンが同一タイトル内に見つかる)
                                      → product (full_title_overlap)
  - coverage == 0.0 (どのタイトルにも1トークンも見つからない)
                                      → non_product (zero_title_overlap)
      ジャンルは通ったが検索語との関連が1つも確認できない = 返っているのは
      クエリと無関係な商品だけ、という強いシグナル。実例:「みみっち」で
      返る商品がすべて「たまごっち本体」を指すタイトルで、「みみっち」という
      トークンがどれにも現れない場合。
  - 0 < coverage < 1.0               → ambiguous (partial_title_overlap)
      一部のトークンだけ一致。初版 (2026-08-10) はここを non_product に固定
      していたが、owner レビューで「一部のトークンがタイトル表記と揺れる
      正当な商品語 (語順違い・別表記の複合語等) を誤って non_product に落とす
      おそれがある」との指摘を受けて変更した。coverage の根拠は「クエリを
      空白区切りにした表層トークン」であり、意味的な同義語・語順違い
      (例: 「オルゴールメリー」というクエリ語に対しタイトルが「メリー
      オルゴール」と逆順の複合語で書かれている場合) を検出できない。この
      検出限界がある以上、部分一致は「たまごっち 種類」のような真の非商品
      パターンと、表記揺れで一部だけ一致しなかった真の商品パターンの
      **両方を含みうる**。区別する根拠が無い状態で non_product に倒すと、
      表記揺れ側の実在商品を機械的に握り潰すことになり、これは L1 の
      store_navigational で「ボーネルンド おもちゃ」を誤除外していたのと
      同じ種類の事故になる。したがって部分一致は ambiguous にして owner の
      目視レビューに回す。
  - API 例外                          → ambiguous (api_error)
      判定材料が無いので不明。product にも non_product にも潰さない。

  この非対称設計 (「ambiguous を product にも non_product にも潰さない」) は
  #4892 の L1 gate と同じ規律に従う。断定できない語は次の判断者 (人間) に
  渡す。verdict のうち non_product だけが「確認材料が十分にある」場合
  (hits=0 / genre_pass_hits=0 / coverage=0 の3パターン) に限定されている
  ことに注意 (部分一致は確認材料が不十分なので non_product ではない)。

制約 (scripts/probe_demand_supply.py の流儀を踏襲):
  - 1 keyword あたり SearchItems 1 回、1.1 秒間隔
  - API 失敗は keyword 単位で記録して次へ進む (1 語の失敗で全体を落とさない)
  - **data/raw/amazon.json を書かない** = Jules の生成プールに一切入らない
    = 記事は 1 本も作られない
  - --dry-run で API を呼ばず対象語の確認だけできる

レスポンス構造:
  SearchItems の実レスポンスは searchResult.items (probe_demand_supply.py の
  実測どおり)。browse_nodes の取り出しは fetch_amazon.extract_browse_nodes を
  そのまま再利用する (browseNodeInfo.browseNodes の ancestor チェーン走査を
  自前実装しない。2026-08-10 に searchResult.items の読み違いで 98/98 が
  0 件になった事故があるため、レスポンス構造は必ず既存実装から借りる)。
  resources も fetch_amazon.SEARCH_ITEM_RESOURCES (dry-run gate で実証済み)
  をそのまま使う。

使い方:
    python scripts/probe_ubersuggest_products.py --dry-run --limit 200
    python scripts/probe_ubersuggest_products.py --limit 200   # secrets が要る
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import fetch_amazon as FA  # noqa: E402  browse_nodes 抽出・resources を再利用する
from genre_gate import classify_genre  # noqa: E402  再実装しない

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_ubersuggest_products")

DEFAULT_DEMAND_PATH = "data/analytics/ubersuggest_demand.json"
DEFAULT_OUT = "data/analytics/ubersuggest_product_probe.json"
DEFAULT_SEARCH_INDEX = "Toys"
DEFAULT_ITEM_COUNT = 10
DEFAULT_LIMIT = 200
SLEEP_SECONDS = 1.1

# fetch_amazon.SEARCH_ITEM_RESOURCES は 04-validate-article-pr.yml の dry-run
# gate (Issue #785) で実証済みの resource セット。itemInfo.title と
# browseNodeInfo.browseNodes(.ancestor) が既に含まれているので、新規に
# resource 名を作らずそのまま流用する (無効な resource は全 keyword 400 に
# なった実績があるため、実証済みのもの以外を自分で組み立てない)。
SEARCH_RESOURCES = FA.SEARCH_ITEM_RESOURCES

FULL_COVERAGE = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").casefold()


def extract_items(response: Any) -> list[dict[str, Any]]:
    """SearchItems レスポンスから [{asin, title, browse_nodes}] を返す。

    構造判断は probe_demand_supply.extract_asins と同じ (searchResult.items、
    無ければトップレベル items にフォールバック)。browse_nodes は
    fetch_amazon.extract_browse_nodes をそのまま呼ぶ (自前実装しない)。
    形が違う/空のときは空リストを返す (例外にしない)。
    """
    if not isinstance(response, dict):
        return []
    items = response.get("searchResult", {})
    items = items.get("items") if isinstance(items, dict) else None
    if not isinstance(items, list):
        # 念のためトップレベルも見る (クライアント側で平坦化された場合)
        items = response.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        asin = it.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        title = FA._safe_get(it, "itemInfo", "title", "displayValue") or ""
        browse_nodes = FA.extract_browse_nodes(it)
        out.append({"asin": asin, "title": title, "browse_nodes": browse_nodes})
    return out


def compute_title_overlap(raw_query: str, titles: list[str]) -> float:
    """raw_query のトークン (空白区切り) が titles (genre_pass_hits のタイトル
    のみ) のうちどれか1つにどれだけ含まれるかを 0.0〜1.0 で返す。

    titles が空 (genre_pass_hits=0) なら 0.0。NFKC + casefold で正規化した
    うえで部分一致を見る。judge_verdict のしきい値と対で使うこと。
    """
    tokens = [t for t in re.split(r"\s+", (raw_query or "").strip()) if t]
    if not tokens or not titles:
        return 0.0
    best = 0
    for title in titles:
        norm_title = _normalize_text(title)
        matched = sum(1 for t in tokens if _normalize_text(t) in norm_title)
        if matched > best:
            best = matched
        if best == len(tokens):
            break
    return best / len(tokens)


def judge_verdict(hits: int, genre_pass_hits: int, coverage: float) -> tuple[str, str]:
    """(verdict, reason) を返す。しきい値の根拠はモジュール docstring 参照。

    2026-08-10 owner レビューで partial/zero の割り当てを入れ替えた:
      - coverage == 0.0 (どのタイトルにも1トークンも無い) → non_product
        (無関係な商品しか返っていない、という強いシグナル)
      - 0 < coverage < 1.0 (一部だけ一致) → ambiguous
        (表記揺れ・語順違いで一致しなかった実在商品を巻き込みうるため、
        断定せず人間のレビューに回す)
    """
    if hits == 0:
        return "non_product", "no_hits"
    if genre_pass_hits == 0:
        return "non_product", "no_genre_pass"
    if coverage >= FULL_COVERAGE:
        return "product", "full_title_overlap"
    if coverage <= 0.0:
        return "non_product", "zero_title_overlap"
    return "ambiguous", "partial_title_overlap"


def probe_keyword(api, query: str, raw_query: str, volume: float, sites: list[str],
                  search_index: str, item_count: int) -> dict[str, Any]:
    """1 語を検索して verdict を確定する。API 例外は error として畳んで返す
    (verdict は ambiguous。判定材料が無いので product/non_product どちらにも
    倒さない)。
    """
    try:
        res = api.search_items(keywords=raw_query, search_index=search_index,
                               item_count=item_count, item_page=1,
                               resources=SEARCH_RESOURCES)
    except Exception as e:  # API 側の例外型に依存しない (1 語の失敗で全体を止めない)
        return {
            "query": query, "raw_query": raw_query, "volume": volume, "sites": sites,
            "error": f"{type(e).__name__}: {e}",
            "hits": 0, "genre_pass_hits": 0, "title_overlap": 0.0,
            "verdict": "ambiguous", "verdict_reason": "api_error",
            "sample_titles": [],
        }

    items = extract_items(res)
    hits = len(items)
    passing = [it for it in items if classify_genre(it["browse_nodes"], it["asin"])[0] == "pass"]
    genre_pass_hits = len(passing)
    coverage = compute_title_overlap(raw_query, [it["title"] for it in passing])
    verdict, reason = judge_verdict(hits, genre_pass_hits, coverage)

    return {
        "query": query, "raw_query": raw_query, "volume": volume, "sites": sites,
        "error": None,
        "hits": hits, "genre_pass_hits": genre_pass_hits,
        "title_overlap": round(coverage, 3),
        "verdict": verdict, "verdict_reason": reason,
        "sample_titles": [it["title"] for it in items[:5]],
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    product = [r for r in results if r["verdict"] == "product"]
    non_product = [r for r in results if r["verdict"] == "non_product"]
    ambiguous = [r for r in results if r["verdict"] == "ambiguous"]
    errors = [r for r in results if r["error"]]
    return {
        "keywords_probed": len(results),
        "product": len(product),
        "non_product": len(non_product),
        "ambiguous": len(ambiguous),
        "error_count": len(errors),
        "product_volume_sum": sum(r["volume"] or 0 for r in product),
    }


def load_targets(demand_path: pathlib.Path, limit: int) -> list[dict[str, Any]]:
    payload = json.loads(demand_path.read_text(encoding="utf-8"))
    entries = payload.get("keywords") or []
    entries = sorted(entries, key=lambda e: -(e.get("volume") or 0))
    if limit > 0:
        entries = entries[:limit]
    return entries


def run(demand_path: pathlib.Path, out_path: pathlib.Path, limit: int, search_index: str,
        item_count: int, dry_run: bool, api=None, sleeper=time.sleep) -> dict[str, Any]:
    entries = load_targets(demand_path, limit)
    params = {"limit": limit, "search_index": search_index, "item_count": item_count}

    if dry_run:
        logger.info("[dry-run] API を呼ばずに終了する。対象語 %d 件", len(entries))
        for e in entries[:20]:
            logger.info("  %-24s volume=%s sites=%s", e["query"][:24], e.get("volume"),
                        ",".join(e.get("sites") or []))
        return {
            "generated_at": _now_iso(),
            "params": params,
            "summary": {"keywords_probed": 0},
            "results": [],
            "targets": [
                {"query": e["query"], "raw_query": e["raw_query"], "volume": e.get("volume"),
                 "sites": e.get("sites") or []}
                for e in entries
            ],
        }

    if api is None:
        from creators_api_client import CreatorsAPIClient  # 遅延 import (dry-run では不要)
        api = CreatorsAPIClient()

    results: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        r = probe_keyword(api, e["query"], e["raw_query"], e.get("volume") or 0,
                          e.get("sites") or [], search_index, item_count)
        results.append(r)
        logger.info("  [%3d/%3d] %-24s hits=%2d genre_pass=%2d overlap=%.2f verdict=%-11s %s",
                    i + 1, len(entries), e["query"][:24], r["hits"], r["genre_pass_hits"],
                    r["title_overlap"], r["verdict"], r["error"] or "")
        if i + 1 < len(entries):
            sleeper(SLEEP_SECONDS)

    summary = summarize(results)
    report = {"generated_at": _now_iso(), "params": params, "summary": summary, "results": results}

    logger.info("product %d 語 (volume合計 %d) / non_product %d 語 / ambiguous %d 語 / "
                "エラー %d 語",
                summary["product"], summary["product_volume_sum"], summary["non_product"],
                summary["ambiguous"], summary["error_count"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out_path)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ubersuggest 需要語の Amazon 実査 (観測のみ, #2686 PR-D)")
    ap.add_argument("--demand", default=DEFAULT_DEMAND_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Volume上位N語 (0=全件)")
    ap.add_argument("--search-index", default=DEFAULT_SEARCH_INDEX)
    ap.add_argument("--item-count", type=int, default=DEFAULT_ITEM_COUNT)
    ap.add_argument("--dry-run", action="store_true", help="API を一切呼ばない")
    args = ap.parse_args()

    run(pathlib.Path(args.demand), pathlib.Path(args.out), args.limit, args.search_index,
        args.item_count, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
