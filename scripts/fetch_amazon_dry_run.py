#!/usr/bin/env python3
"""Creator API resources dry-run gate (Issue #785).

`scripts/fetch_amazon.py` の SEARCH_ITEM_RESOURCES 配列をそのまま使って Creator API
の searchItems を 1 keyword × 1 item で投げ、HTTP 200 + items[] != [] を確認する。

PR #761 が "offersV2.listings.deliveryInfo" を入れて Creator API が全 keyword で
400 を返し fetch_amazon cron が死亡 (PR #783 で hotfix) した再発を防ぐため、
04-validate-article-pr.yml で fetch_amazon.py / creators_api_client.py を触る PR
に対し本スクリプトを必須 gate として走らせる。

Quota: 1 OAuth token + 1 searchItems = 極小。

Exit codes:
  0 — 200 OK かつ items[] に 1 件以上が返った (resources 全 valid と判定)
  1 — API 接続失敗 / 200 以外 / items[] 空 / 環境変数欠落
"""
from __future__ import annotations

import argparse
import os
import sys

from creators_api_client import CreatorsAPIClient, CreatorsAPIError
from fetch_amazon import SEARCH_ITEM_RESOURCES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keyword",
        default="知育玩具",
        help="searchItems に投げる keyword (default: 知育玩具)",
    )
    parser.add_argument(
        "--search-index",
        default="Toys",
        help="searchItems の searchIndex (default: Toys)",
    )
    parser.add_argument(
        "--item-count",
        type=int,
        default=1,
        help="取得件数 (default: 1, quota 節約)",
    )
    args = parser.parse_args()

    required = [
        "AMAZON_CREATORS_APPLICATION_ID",
        "AMAZON_CREATORS_CREDENTIAL_ID",
        "AMAZON_CREATORS_CREDENTIAL_SECRET",
        "AMAZON_PARTNER_TAG",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"[dry-run] FAIL: required env vars missing: {missing}", flush=True)
        return 1

    print(f"[dry-run] resources ({len(SEARCH_ITEM_RESOURCES)}):", flush=True)
    for r in SEARCH_ITEM_RESOURCES:
        print(f"  - {r}", flush=True)
    print(
        f"[dry-run] searchItems(keywords={args.keyword!r}, "
        f"searchIndex={args.search_index!r}, itemCount={args.item_count})",
        flush=True,
    )

    api = CreatorsAPIClient()
    try:
        result = api.search_items(
            keywords=args.keyword,
            search_index=args.search_index,
            item_count=args.item_count,
            item_page=1,
            resources=SEARCH_ITEM_RESOURCES,
        )
    except CreatorsAPIError as e:
        print(f"[dry-run] FAIL: CreatorsAPIError: {e}", flush=True)
        return 1
    except Exception as e:
        print(f"[dry-run] FAIL: unexpected {type(e).__name__}: {e}", flush=True)
        return 1

    # Creator API は invalid resource をエラー扱いせず items[] 空で返すことが
    # あるため、items の有無を必ず確認する (PR #761 の cron 死亡パターン)。
    # searchItems の応答は searchResult.items に入る (getItems は itemsResult.items)。
    items = (result or {}).get("searchResult", {}).get("items") or []
    errors = (result or {}).get("errors") or []

    if errors:
        print(f"[dry-run] FAIL: API returned errors: {errors}", flush=True)
        return 1

    if not items:
        print(
            "[dry-run] FAIL: items[] empty — resources 配列に invalid な名前が混ざっている可能性。"
            " creators_api_client._attempt_request の HTTP ログを確認してください。",
            flush=True,
        )
        return 1

    asin = items[0].get("asin", "(no asin)")
    print(f"[dry-run] OK: got {len(items)} item(s), first asin={asin}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
