#!/usr/bin/env python3
"""Buffer API へ X / Threads の単発 post を作成する (Issue #1420 path α)。

#1420 検証で Buffer GraphQL は X (twitter) ・ Threads ともに thread 配信不可と
判明したため、両 channel とも「hook + 改行 + URL」の単発 post に統一する。
X 公式 API 直叩き経路は pay-per-usage 化 (URL 含み $0.20/req) でコスト合わず
見送り。本スクリプトが商品 SNS 配信の唯一経路。

使い方:
    BUFFER_ACCESS_TOKEN=xxxx python scripts/notify_buffer.py B0DBTLH8ZM
    BUFFER_ACCESS_TOKEN=xxxx python scripts/notify_buffer.py B0DBTLH8ZM --live   # draft でなく queue 投入

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
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No article JSON for ASIN={asin_upper} under {ARTICLES_DIR}")
    with open(matches[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def build_single_payload(article: dict, base_url: str) -> tuple[str, str]:
    """単発 post 用に (本文, URL) を返す。本文には URL を末尾同梱する。

    X の上限 280 文字に収めるため hook を 220 文字で truncate (URL + 改行2つ +
    余白で約 60 文字消費するため)。Threads は 500 文字なので余裕。
    """
    asin = article.get("slug", "").rsplit("-", 1)[-1].lower()
    title = article.get("title") or ""
    desc = (article.get("meta_description") or "").strip()
    url = f"{base_url}/products/{asin}/"
    hook = desc if desc else title
    if len(hook) > 220:
        hook = hook[:219].rstrip() + "…"
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
