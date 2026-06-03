#!/usr/bin/env python3
"""Threads (Meta Graph API v1.0) へ thread 構成で投稿する (Issue #1420 path A)。

Buffer の Threads channel は thread 配信非対応のため、Threads 公式 API 直叩きで
2 投構成 (1投目=hook、2投目=URL を reply_to_id で連結) を実装する。X は Buffer
側で単発投稿を継続。

Meta Graph Threads API の投稿フロー (v1.0):
  1. POST /{user_id}/threads          → container ID (media_type=TEXT)
  2. POST /{user_id}/threads_publish  → 第1投の thread ID (公開)
  3. POST /{user_id}/threads          → reply container ID (reply_to_id=第1投)
  4. POST /{user_id}/threads_publish  → 第2投の thread ID (公開)

使い方:
    THREADS_ACCESS_TOKEN=xxxx THREADS_USER_ID=12345 \\
        python scripts/notify_threads.py B0DBTLH8ZM
    THREADS_ACCESS_TOKEN=xxxx THREADS_USER_ID=12345 \\
        python scripts/notify_threads.py B0DBTLH8ZM --dry-run   # API は叩かない

env:
    THREADS_ACCESS_TOKEN   必須 (long-lived 60 日)
    THREADS_USER_ID        必須 (例: 26988001024213928)
    OMOCHA_BASE_URL        default: https://navi.omcha.jp

exit code:
    0 = 両方の publish 成功
    1 = API エラー (post1 失敗 = 完全失敗 / post2 失敗 = post1 だけ公開済み)
    2 = env 不足
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows cp932 で emoji 含む preview を print する際の UnicodeEncodeError 対策。
# CI (Linux UTF-8) では no-op。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

GRAPH_BASE = "https://graph.threads.net/v1.0"
DEFAULT_BASE_URL = "https://navi.omcha.jp"

# 2026-06-03 (#1494 follow-up): Meta 公式 docs / コミュニティ報告では container
# 作成後 publish まで **最低 30 秒** 待つことを推奨 (text-only でも eventual
# consistency window あり)。reply container (reply_to_id 付き) は特に出やすい。
# 旧実装は 0 秒待ち + 1 回 retry のみで session 89 run 26862829282 で
# post2 が 4279009 "Media Not Found" で失敗した。
PUBLISH_SETTLE_SECONDS = 32         # container 作成後 → publish 前の固定 sleep
PUBLISH_RETRY_SECONDS = 10          # retry 間隔
PUBLISH_MAX_RETRIES = 3             # 失敗時の最大 retry 回数
# eventual consistency window 中に出る既知 error message variants (case-insensitive 含む)
PUBLISH_TRANSIENT_ERROR_KEYS = (
    "media id is not available",
    "media not found",
    "cannot be found",
    "does not exist",
    "the requested resource does not exist",
    "4279009",
    "4279019",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"


def load_article(asin: str) -> dict:
    asin_upper = asin.upper()
    pattern = str(ARTICLES_DIR / f"*-{asin_upper}.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No article JSON for ASIN={asin_upper} under {ARTICLES_DIR}")
    with open(matches[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def build_payload(article: dict, base_url: str) -> tuple[str, str, str]:
    """(post1 hook, post2 url, canonical url) を返す。

    Threads は 500 文字制限なので X (280) より緩いが、視認性のため 1 投目を
    480 文字で truncate。2 投目は URL のみ — Threads 側が link preview card
    (OG image + 商品 title) を自前で描画するため、本文に「詳しくはこちら 👇」
    のような導線を付けると preview card の title と二重表示になり冗長 (#1500)。
    """
    asin = article.get("slug", "").rsplit("-", 1)[-1].lower()
    title = article.get("title") or ""
    desc = (article.get("meta_description") or "").strip()
    url = f"{base_url}/products/{asin}/"
    hook = desc if desc else title
    if len(hook) > 480:
        hook = hook[:479].rstrip() + "…"
    post2 = url
    return hook, post2, url


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"raw": "<unparseable>"}
        return {"_http_error": {"status": e.code, "body": err_body}}


def create_container(user_id: str, token: str, text: str, reply_to_id: str | None = None) -> dict:
    url = f"{GRAPH_BASE}/{user_id}/threads"
    data = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token,
    }
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    return _post(url, data)


def _is_transient_publish_error(resp: dict) -> bool:
    if "_http_error" not in resp:
        return False
    blob = json.dumps(resp, ensure_ascii=False).lower()
    return any(key in blob for key in PUBLISH_TRANSIENT_ERROR_KEYS)


def publish_container(user_id: str, token: str, creation_id: str) -> dict:
    """container を publish。eventual consistency 失敗時は最大 3 回 retry。"""
    url = f"{GRAPH_BASE}/{user_id}/threads_publish"
    data = {"creation_id": creation_id, "access_token": token}
    # Meta 公式推奨に従い container 作成直後の publish は 30s 以上 settle 待ち。
    # ここに到達した時点で create_container の往復で数秒経過しているが、明示的に
    # 残り分を sleep する。
    print(f"[publish] settle wait {PUBLISH_SETTLE_SECONDS}s before publish (creation_id={creation_id})",
          flush=True)
    time.sleep(PUBLISH_SETTLE_SECONDS)
    resp = _post(url, data)
    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        if not _is_transient_publish_error(resp):
            return resp
        print(
            f"[publish] transient error on attempt {attempt}/{PUBLISH_MAX_RETRIES}, "
            f"sleeping {PUBLISH_RETRY_SECONDS}s and retrying: {json.dumps(resp, ensure_ascii=False)}",
            flush=True,
        )
        time.sleep(PUBLISH_RETRY_SECONDS)
        resp = _post(url, data)
    return resp


def _report(label: str, resp: dict) -> str | None:
    """成功時は ID を返す。失敗時は None を返し stderr に出力。"""
    if "_http_error" in resp:
        err = resp["_http_error"]
        print(f"[{label}] FAIL HTTP {err['status']}: {json.dumps(err['body'], ensure_ascii=False)}",
              file=sys.stderr)
        return None
    rid = resp.get("id")
    if not rid:
        print(f"[{label}] FAIL no id in response: {resp}", file=sys.stderr)
        return None
    print(f"[{label}] OK id={rid}")
    return rid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asin", help="Amazon ASIN")
    parser.add_argument("--dry-run", action="store_true", help="API は叩かず preview だけ")
    args = parser.parse_args()

    token = os.environ.get("THREADS_ACCESS_TOKEN")
    user_id = os.environ.get("THREADS_USER_ID")
    base_url = os.environ.get("OMOCHA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    article = load_article(args.asin)
    post1, post2, url = build_payload(article, base_url)

    print(f"--- preview (ASIN={args.asin.upper()}) ---")
    print(f"[Threads 1投目] ({len(post1)} chars / limit 500)\n{post1}\n")
    print(f"[Threads 2投目] ({len(post2)} chars)\n{post2}\n")
    print(f"[URL] {url}")
    print(f"[mode] {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("---")

    if args.dry_run:
        return 0

    if not token or not user_id:
        print("THREADS_ACCESS_TOKEN and THREADS_USER_ID env vars required", file=sys.stderr)
        return 2

    # 1 投目 container 作成 + publish
    c1 = create_container(user_id, token, post1)
    cid1 = _report("post1.container", c1)
    if not cid1:
        return 1
    p1 = publish_container(user_id, token, cid1)
    pid1 = _report("post1.publish", p1)
    if not pid1:
        return 1

    # 2 投目 container 作成 (reply_to_id=post1 の publish id) + publish
    c2 = create_container(user_id, token, post2, reply_to_id=pid1)
    cid2 = _report("post2.container", c2)
    if not cid2:
        print(f"::warning::post1 は公開済み (id={pid1}) だが post2 container 失敗", file=sys.stderr)
        return 1
    p2 = publish_container(user_id, token, cid2)
    pid2 = _report("post2.publish", p2)
    if not pid2:
        print(f"::warning::post1 は公開済み (id={pid1}) だが post2 publish 失敗", file=sys.stderr)
        return 1

    print(f"thread published: {pid1} → {pid2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
