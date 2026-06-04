#!/usr/bin/env python3
"""Buffer API へ X / Threads の単発 post を作成する (Issue #1420 path A)。

path A 設計 (session 85 → 86):
  - Buffer は **X 単発投稿のみ** を商品配信で使う (workflow から `--x-only`)
  - Threads は scripts/notify_threads.py が Meta Graph API で 2 投 thread 化
  - Buffer Threads channel は手動の日常投稿用に残置 (workflow からは投げない)

ただし本スクリプト自体は X / Threads 両 channel への投稿を引き続きサポート
する (`--threads-only` で Buffer Threads のみ等)。商品 cron は --x-only 固定。

使い方:
    BUFFER_ACCESS_TOKEN=xxxx python scripts/notify_buffer.py B0DBTLH8ZM
    BUFFER_ACCESS_TOKEN=xxxx python scripts/notify_buffer.py B0DBTLH8ZM --live    # draft → queue
    BUFFER_ACCESS_TOKEN=xxxx python scripts/notify_buffer.py B0DBTLH8ZM --x-only --live  # workflow が使う形

env:
    BUFFER_ACCESS_TOKEN          必須
    BUFFER_X_CHANNEL_ID          default: 67a022e330a138f0dbdfadbd
    BUFFER_THREADS_CHANNEL_ID    default: 68c0f42b76363a8367bc5408
    OMOCHA_BASE_URL              default: https://navi.omcha.jp
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import sns_copy_store
from _x_text import X_LIMIT, truncate_to_weight, x_weight

GRAPHQL_URL = "https://api.buffer.com/"
DEFAULT_X_CHANNEL_ID = "67a022e330a138f0dbdfadbd"
DEFAULT_THREADS_CHANNEL_ID = "68c0f42b76363a8367bc5408"
DEFAULT_BASE_URL = "https://navi.omcha.jp"

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id } }
    ... on InvalidInputError { message }
    ... on UnauthorizedError { message }
    ... on LimitReachedError { message }
    ... on RestProxyError    { message link code }
    ... on NotFoundError     { message }
    ... on UnexpectedError   { message }
  }
}
""".strip()


def load_article(asin: str) -> dict:
    asin_upper = asin.upper()
    pattern = str(ARTICLES_DIR / f"*-{asin_upper}.json")
    # サイドカー (.quality/.enrichment/.seo.json) は product を持たず、かつ
    # ".quality.json" > ".json" のため sorted()[-1] で本体を shadow する。除外必須。
    matches = sorted(
        m for m in glob.glob(pattern)
        if not m.endswith((".enrichment.json", ".quality.json", ".seo.json"))
    )
    if not matches:
        raise FileNotFoundError(f"No article JSON for ASIN={asin_upper} under {ARTICLES_DIR}")
    with open(matches[-1], "r", encoding="utf-8") as f:
        return json.load(f)


# weighted char count ヘルパは scripts/_x_text.py に共有化 (session 95)。
# 既存呼出 (_x_weight / _truncate_to_weight) は alias で互換維持。
_x_weight = x_weight
_truncate_to_weight = truncate_to_weight

# "\n\n→ " = 4 weight (Latin), 末尾「…」追加分 +2 (CJK weight 2 相当の安全枠)
X_SEPARATOR_WEIGHT = 4
X_ELLIPSIS_WEIGHT = 2
# 余裕 5 weight を確保 (実測との誤差吸収)
X_SAFETY = 5


def build_single_payload(article: dict, base_url: str) -> tuple[str, str]:
    """単発 post 用に (本文, URL) を返す。本文には URL を末尾同梱する。

    Buffer は X URL を t.co 短縮 (23 chars) ではなく **actual chars** (例:
    /products/{asin}/ = 43 chars) で数えるため、URL 長を動的に計算して
    budget を決める。X 280 char 制限は CJK weight 2 で評価されるので
    `_x_weight` ベースで hook を切る。
    """
    asin = article.get("slug", "").rsplit("-", 1)[-1].lower()
    title = article.get("title") or ""
    desc = (article.get("meta_description") or "").strip()
    url = f"{base_url}/products/{asin}/"
    url_weight = sum(_x_weight(c) for c in url)
    hook_budget = X_LIMIT - url_weight - X_SEPARATOR_WEIGHT - X_ELLIPSIS_WEIGHT - X_SAFETY
    # #1580 Part 2: Jules 事前生成の SNS コピーが有れば優先。無ければ従来 fallback。
    # truncate は store コピーにも安全網として残す (限界超過時の保険)。
    store_hook = sns_copy_store.get_hook(asin, "x")
    hook = store_hook or (desc if desc else title)
    hook, truncated = _truncate_to_weight(hook, hook_budget)
    if truncated:
        hook = hook.rstrip() + "…"
    text = f"{hook}\n\n→ {url}"
    return text, url


def graphql_request(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}]}


def create_post(token: str, channel_id: str, service: str, text: str, live: bool) -> dict:
    # X (twitter) / Threads とも単発 post。metadata は service 別に最低限。
    if service == "twitter":
        metadata = {"twitter": {"thread": []}}
    elif service == "threads":
        metadata = {"threads": {"type": "post"}}
    else:
        raise ValueError(f"Unsupported service: {service}")

    variables = {
        "input": {
            "channelId": channel_id,
            "text": text,
            "assets": [],
            "schedulingType": "automatic",
            "mode": "addToQueue",
            "saveToDraft": not live,
            "source": "omochairo-notify-buffer",
            "aiAssisted": False,
            "metadata": metadata,
        }
    }
    return graphql_request(token, CREATE_POST_MUTATION, variables)


def report(result: dict, label: str) -> bool:
    if "errors" in result:
        print(f"[{label}] FAIL: {result['errors']}", file=sys.stderr)
        return False
    data = (result.get("data") or {}).get("createPost") or {}
    typename = data.get("__typename")
    if typename == "PostActionSuccess":
        print(f"[{label}] OK: post.id={data['post']['id']}")
        return True
    print(f"[{label}] FAIL: {typename}: {data.get('message') or data}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asin", help="Amazon ASIN (case-insensitive)")
    parser.add_argument("--live", action="store_true", help="addToQueue 投入 (default: draft)")
    parser.add_argument("--x-only", action="store_true")
    parser.add_argument("--threads-only", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("BUFFER_ACCESS_TOKEN")
    if not token:
        print("BUFFER_ACCESS_TOKEN env var required", file=sys.stderr)
        return 2

    x_id = os.environ.get("BUFFER_X_CHANNEL_ID", DEFAULT_X_CHANNEL_ID)
    threads_id = os.environ.get("BUFFER_THREADS_CHANNEL_ID", DEFAULT_THREADS_CHANNEL_ID)
    base_url = os.environ.get("OMOCHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    article = load_article(args.asin)
    text, url = build_single_payload(article, base_url)
    print(f"--- preview (ASIN={args.asin.upper()}) ---")
    print(f"[X / Threads 単発]\n{text}\n")
    print(f"[URL] {url}")
    print(f"[chars] X={len(text)} (limit 280) / Threads={len(text)} (limit 500)")
    print(f"[mode] {'LIVE (addToQueue)' if args.live else 'DRAFT'}")
    print("---")

    ok = True
    if not args.threads_only:
        ok &= report(create_post(token, x_id, "twitter", text, args.live), "X")
    if not args.x_only:
        ok &= report(create_post(token, threads_id, "threads", text, args.live), "Threads")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
