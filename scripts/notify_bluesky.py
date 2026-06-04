#!/usr/bin/env python3
"""Bluesky (AT Protocol) へ link card 付きで商品記事を投稿する (Issue #1598)。

X (Buffer 単発) / Threads (Meta Graph thread) に続く 3 つ目の配信チャネル。
Bluesky は分散型 SNS で X のインプレ低下対策の受け皿 + 新規流入チャネル。

X / Threads と違い、Bluesky のリンクカード (external embed) は **クライアント側で
組み立てる** 必要がある (サーバが投稿時に OGP を自動取得しない)。よって本スクリプトが
OG 画像を取得 → uploadBlob → external embed の thumb に差し込む。URL は embed が
持つので本文 (text) には URL を含めない (X のように本文末尾へ URL 同梱は不要)。

AT Protocol 投稿フロー (追加依存なし、urllib のみ):
  1. POST /xrpc/com.atproto.server.createSession  → accessJwt + did
  2. GET  {og_image_url}                           → 画像 bytes (失敗時 thumb 省略)
  3. POST /xrpc/com.atproto.repo.uploadBlob        → blob ref
  4. POST /xrpc/com.atproto.repo.createRecord      → post の uri/cid (公開)

使い方:
    BLUESKY_HANDLE=xxx.bsky.social BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \\
        python scripts/notify_bluesky.py B0DBTLH8ZM
    python scripts/notify_bluesky.py B0DBTLH8ZM --dry-run   # API は叩かない

env:
    BLUESKY_HANDLE         必須 (例: omochairo.bsky.social)
    BLUESKY_APP_PASSWORD   必須 (App Password。本体 PW ではなく専用発行のもの)
    BLUESKY_PDS            default: https://bsky.social
    OMOCHA_BASE_URL        default: https://navi.omcha.jp

exit code:
    0 = 投稿成功 (または --dry-run)
    1 = API エラー
    2 = env 不足 (secret 未設定時の inert no-op はこの経路。workflow は continue-on-error)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows cp932 で emoji 含む preview を print する際の UnicodeEncodeError 対策。
# CI (Linux UTF-8) では no-op。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import sns_copy_store  # noqa: E402  (stdout reconfigure を先に済ませる)

DEFAULT_PDS = "https://bsky.social"
DEFAULT_BASE_URL = "https://navi.omcha.jp"

# Bluesky 本文上限は 300 graphemes。日本語は概ね 1 文字 = 1 grapheme なので
# 文字数ベースで安全側に切る。URL は embed card が持つため本文には含めない。
BLUESKY_TEXT_LIMIT = 300
# external embed の title / description はカード表示用。長すぎは無意味なので抑える。
EMBED_TITLE_LIMIT = 120
EMBED_DESC_LIMIT = 200
# uploadBlob は画像 1MB 上限。超過時は thumb を省略してカードのみ投稿する。
BLOB_MAX_BYTES = 1_000_000

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"


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


def _truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def build_payload(article: dict, base_url: str) -> dict:
    """投稿に必要な (text, url, title, desc, og_image_url, fallback_image) を返す。

    hook は Threads 用 store コピー (URL 非同梱・カード前提で Bluesky と最も近い)
    を優先し、無ければ meta_description / title に fallback する。
    """
    asin = article.get("slug", "").rsplit("-", 1)[-1].lower()
    title = article.get("title") or ""
    desc = (article.get("meta_description") or "").strip()
    url = f"{base_url}/products/{asin}/"
    # #1580: Jules 事前生成コピーは x/threads のみ存在。Bluesky は threads 流用。
    store_hook = sns_copy_store.get_hook(asin, "threads")
    hook = store_hook or (desc if desc else title)
    product = article.get("product") or {}
    fallback_image = product.get("image") or ""
    return {
        "text": _truncate(hook, BLUESKY_TEXT_LIMIT),
        "url": url,
        "title": _truncate(title, EMBED_TITLE_LIMIT),
        "desc": _truncate(desc, EMBED_DESC_LIMIT),
        "og_image_url": f"{base_url}/og/{asin}.jpg",
        "fallback_image": fallback_image,
    }


def _request(url: str, *, data: bytes | None, headers: dict, method: str = "POST") -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"raw": "<unparseable>"}
        return {"_http_error": {"status": e.code, "body": err_body}}


def create_session(pds: str, handle: str, password: str) -> dict:
    return _request(
        f"{pds}/xrpc/com.atproto.server.createSession",
        data=json.dumps({"identifier": handle, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _fetch_image(*candidates: str) -> tuple[bytes, str] | None:
    """候補 URL を順に試し、最初に取れた (bytes, content_type) を返す。全滅で None。"""
    for url in candidates:
        if not url:
            continue
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "omochairo-notify-bluesky/1.0 (+https://navi.omcha.jp)"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                blob = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        except Exception as e:
            print(f"[thumb] fetch failed for {url}: {e}", file=sys.stderr)
            continue
        if not blob:
            continue
        if len(blob) > BLOB_MAX_BYTES:
            print(f"[thumb] skip {url}: {len(blob)} bytes > {BLOB_MAX_BYTES} limit", file=sys.stderr)
            continue
        return blob, (ctype if ctype.startswith("image/") else "image/jpeg")
    return None


def upload_blob(pds: str, token: str, blob: bytes, content_type: str) -> dict:
    return _request(
        f"{pds}/xrpc/com.atproto.repo.uploadBlob",
        data=blob,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {token}"},
    )


def build_record(payload: dict, thumb_blob: dict | None) -> dict:
    external = {
        "uri": payload["url"],
        "title": payload["title"],
        "description": payload["desc"],
    }
    if thumb_blob:
        external["thumb"] = thumb_blob
    return {
        "$type": "app.bsky.feed.post",
        "text": payload["text"],
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "langs": ["ja"],
        "embed": {"$type": "app.bsky.embed.external", "external": external},
    }


def create_record(pds: str, token: str, did: str, record: dict) -> dict:
    return _request(
        f"{pds}/xrpc/com.atproto.repo.createRecord",
        data=json.dumps(
            {"repo": did, "collection": "app.bsky.feed.post", "record": record}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )


def _report(label: str, resp: dict) -> bool:
    if "_http_error" in resp:
        err = resp["_http_error"]
        print(f"[{label}] FAIL HTTP {err['status']}: {json.dumps(err['body'], ensure_ascii=False)}",
              file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asin", help="Amazon ASIN")
    parser.add_argument("--dry-run", action="store_true", help="API は叩かず preview だけ")
    args = parser.parse_args()

    handle = os.environ.get("BLUESKY_HANDLE")
    password = os.environ.get("BLUESKY_APP_PASSWORD")
    pds = os.environ.get("BLUESKY_PDS", DEFAULT_PDS).rstrip("/")
    base_url = os.environ.get("OMOCHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    article = load_article(args.asin)
    payload = build_payload(article, base_url)

    print(f"--- preview (ASIN={args.asin.upper()}) ---")
    print(f"[Bluesky text] ({len(payload['text'])} chars / limit {BLUESKY_TEXT_LIMIT})\n{payload['text']}\n")
    print(f"[card title] {payload['title']}")
    print(f"[card desc]  {payload['desc']}")
    print(f"[card url]   {payload['url']}")
    print(f"[og image]   {payload['og_image_url']} (fallback: {payload['fallback_image'] or 'none'})")
    print(f"[mode] {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("---")

    if args.dry_run:
        return 0

    if not handle or not password:
        print("BLUESKY_HANDLE and BLUESKY_APP_PASSWORD env vars required", file=sys.stderr)
        return 2

    sess = create_session(pds, handle, password)
    if not _report("createSession", sess):
        return 1
    token = sess.get("accessJwt")
    did = sess.get("did")
    if not token or not did:
        print(f"[createSession] FAIL no accessJwt/did in response: {sess}", file=sys.stderr)
        return 1

    # OG 画像 → blob (失敗しても thumb 無しのカードで続行)
    thumb_blob = None
    fetched = _fetch_image(payload["og_image_url"], payload["fallback_image"])
    if fetched:
        blob_bytes, ctype = fetched
        up = upload_blob(pds, token, blob_bytes, ctype)
        if _report("uploadBlob", up):
            thumb_blob = up.get("blob")
    if not thumb_blob:
        print("::warning::thumb 無しでカード投稿 (OG 画像取得 or uploadBlob 失敗)", file=sys.stderr)

    record = build_record(payload, thumb_blob)
    res = create_record(pds, token, did, record)
    if not _report("createRecord", res):
        return 1

    print(f"posted: uri={res.get('uri')} cid={res.get('cid')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
