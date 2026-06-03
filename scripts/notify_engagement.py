#!/usr/bin/env python3
"""Engagement queue consumer: 1 件 pop して X (Buffer) / Threads (Meta Graph) 配信。

Issue #1495 P1 (session 91) + Issue #1539 (session 94: slot 別 category pick):
  - `data/engagement_queue_x.jsonl` から 1 件 → Buffer X 単発投稿
  - `data/engagement_queue_threads.jsonl` から 1 件 → Meta Graph Threads top-level 単発
  - link 含まない non-link engagement 投稿 (記事 promo は 20-sns-publish.yml が別途配信)
  - --category news|daily で slot に応じた pick logic を切替

選定ロジック (--category=daily の場合、従来挙動):
  1. category == "daily" (None=全カテゴリ許可、互換性 fallback)
  2. published_at is null
  3. earliest_publish_at <= now (null は即時可)
  4. 直近 3 投稿と subcategory が同じものは後回し (rotation 多様性)
  5. 最も id 順 (= created 順) で古いもの (FIFO)

選定ロジック (--category=news の場合):
  1. category == "news"
  2. published_at is null
  3. created_at が now-48h より新しいもの (古いトレンドは配信しない)
  4. earliest_publish_at <= now
  5. latest-first (最も created_at が新しいもの)

配信成功で in-place 更新: published_at + post_id を書き戻し。
候補 0 件の場合は穴開けで skip (queue 枯渇は健全な fail-safe)。

使い方:
    BUFFER_ACCESS_TOKEN=... python scripts/notify_engagement.py --dry-run
    BUFFER_ACCESS_TOKEN=... THREADS_ACCESS_TOKEN=... THREADS_USER_ID=... \\
        python scripts/notify_engagement.py --channels x,threads --category daily --live

env:
    BUFFER_ACCESS_TOKEN    X 配信に必要
    BUFFER_X_CHANNEL_ID    default: 67a022e330a138f0dbdfadbd
    THREADS_ACCESS_TOKEN   Threads 配信に必要 (long-lived 60 日)
    THREADS_USER_ID        Threads 配信に必要
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_X = REPO_ROOT / "data" / "engagement_queue_x.jsonl"
QUEUE_THREADS = REPO_ROOT / "data" / "engagement_queue_threads.jsonl"

BUFFER_GRAPHQL_URL = "https://api.buffer.com/"
BUFFER_DEFAULT_X_CHANNEL_ID = "67a022e330a138f0dbdfadbd"
THREADS_GRAPH_BASE = "https://graph.threads.net/v1.0"

# Threads container 作成後 publish までの settle wait。
# [[feedback-omochairo-threads-publish-settle]]: 0s 即時 publish は 4279009 を踏む。
THREADS_PUBLISH_SETTLE_SECONDS = 32
THREADS_PUBLISH_RETRY_SECONDS = 10
THREADS_PUBLISH_MAX_RETRIES = 3
THREADS_TRANSIENT_KEYS = (
    "media id is not available", "media not found", "cannot be found",
    "does not exist", "the requested resource does not exist",
    "4279009", "4279019",
)

# rotation 多様性: 直近 N 件と同 subcategory を後回し
ROTATION_LOOKBACK = 3

# news スロット候補の鮮度上限 (古いトレンドは配信しない)
NEWS_MAX_AGE_HOURS = 48


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"::warning::Skipping malformed line in {path.name}: {e}", file=sys.stderr)
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Atomic write: 全行書き戻し (in-place 更新の代用)。空 jsonl は空ファイル。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def pick_next(rows: list[dict], now: datetime, category: str | None = None) -> int | None:
    """次に publish すべき row の index を返す。なければ None。

    category 指定で挙動が変わる:
      - "news"  : category=="news" AND created_at > now-48h、最新優先 (latest-first)
      - "daily" : category=="daily"、FIFO + rotation 多様性ペナルティ
      - None    : category 区別なし、従来 (FIFO + rotation) 挙動 (互換性)

    共通条件:
      - published_at is None
      - earliest_publish_at is None or <= now
    """
    recent_subs = [
        r.get("subcategory") for r in rows
        if r.get("published_at")
    ][-ROTATION_LOOKBACK:]

    news_cutoff = now - timedelta(hours=NEWS_MAX_AGE_HOURS)

    pending = []
    for i, r in enumerate(rows):
        if r.get("published_at"):
            continue
        if category is not None and r.get("category") != category:
            continue
        eap = _parse_iso(r.get("earliest_publish_at") or "")
        if eap and eap > now:
            continue
        if category == "news":
            ca = _parse_iso(r.get("created_at") or "")
            if ca is None or ca < news_cutoff:
                continue
        pending.append((i, r))

    if not pending:
        return None

    if category == "news":
        # latest-first: created_at 降順 → tiebreak で id 降順
        def news_key(item: tuple[int, dict]) -> tuple:
            _, r = item
            return (r.get("created_at", ""), r.get("id", ""))
        pending.sort(key=news_key, reverse=True)
        return pending[0][0]

    # daily (or legacy None): FIFO + rotation ペナルティ
    def sort_key(item: tuple[int, dict]) -> tuple:
        _, r = item
        same_sub_penalty = 1 if r.get("subcategory") in recent_subs else 0
        return (same_sub_penalty, r.get("id", ""))

    pending.sort(key=sort_key)
    return pending[0][0]


def _http_post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
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


def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}"}]}


_BUFFER_CREATE_POST = """
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


def post_x_via_buffer(text: str, live: bool) -> tuple[bool, str | None]:
    """Buffer X 単発投稿。成功で (True, post_id) 返す。"""
    token = os.environ.get("BUFFER_ACCESS_TOKEN")
    if not token:
        print("BUFFER_ACCESS_TOKEN env var required for X channel", file=sys.stderr)
        return False, None
    channel_id = os.environ.get("BUFFER_X_CHANNEL_ID", BUFFER_DEFAULT_X_CHANNEL_ID)
    variables = {"input": {
        "channelId": channel_id, "text": text, "assets": [],
        "schedulingType": "automatic", "mode": "addToQueue",
        "saveToDraft": not live, "source": "omochairo-notify-engagement",
        "aiAssisted": False, "metadata": {"twitter": {"thread": []}},
    }}
    result = _http_post_json(
        BUFFER_GRAPHQL_URL,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        {"query": _BUFFER_CREATE_POST, "variables": variables},
    )
    if "errors" in result:
        print(f"[X] FAIL: {result['errors']}", file=sys.stderr)
        return False, None
    data = (result.get("data") or {}).get("createPost") or {}
    if data.get("__typename") == "PostActionSuccess":
        pid = data["post"]["id"]
        print(f"[X] OK: post.id={pid}")
        return True, pid
    print(f"[X] FAIL: {data}", file=sys.stderr)
    return False, None


def _is_threads_transient(resp: dict) -> bool:
    if "_http_error" not in resp:
        return False
    blob = json.dumps(resp, ensure_ascii=False).lower()
    return any(k in blob for k in THREADS_TRANSIENT_KEYS)


def post_threads_top_level(text: str) -> tuple[bool, str | None]:
    """Meta Graph Threads に top-level 単発投稿 (reply_to_id なし)。成功で (True, thread_id)。"""
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    user_id = os.environ.get("THREADS_USER_ID")
    if not token or not user_id:
        print("THREADS_ACCESS_TOKEN and THREADS_USER_ID required for Threads channel",
              file=sys.stderr)
        return False, None

    container_resp = _http_post_form(
        f"{THREADS_GRAPH_BASE}/{user_id}/threads",
        {"media_type": "TEXT", "text": text, "access_token": token},
    )
    if "_http_error" in container_resp or not container_resp.get("id"):
        print(f"[Threads.container] FAIL: {container_resp}", file=sys.stderr)
        return False, None
    cid = container_resp["id"]
    print(f"[Threads.container] OK id={cid}; settle {THREADS_PUBLISH_SETTLE_SECONDS}s before publish",
          flush=True)
    time.sleep(THREADS_PUBLISH_SETTLE_SECONDS)

    publish_url = f"{THREADS_GRAPH_BASE}/{user_id}/threads_publish"
    publish_data = {"creation_id": cid, "access_token": token}
    resp = _http_post_form(publish_url, publish_data)
    for attempt in range(1, THREADS_PUBLISH_MAX_RETRIES + 1):
        if not _is_threads_transient(resp):
            break
        print(f"[Threads.publish] transient attempt {attempt}/{THREADS_PUBLISH_MAX_RETRIES}; "
              f"sleep {THREADS_PUBLISH_RETRY_SECONDS}s and retry: "
              f"{json.dumps(resp, ensure_ascii=False)[:300]}", flush=True)
        time.sleep(THREADS_PUBLISH_RETRY_SECONDS)
        resp = _http_post_form(publish_url, publish_data)
    if "_http_error" in resp or not resp.get("id"):
        print(f"[Threads.publish] FAIL: {resp}", file=sys.stderr)
        return False, None
    pid = resp["id"]
    print(f"[Threads.publish] OK id={pid}")
    return True, pid


def consume_one(queue_path: Path, channel: str, dry_run: bool, live: bool,
                category: str | None = None) -> bool:
    rows = _load_jsonl(queue_path)
    if not rows:
        print(f"[{channel}] queue empty: {queue_path.name}")
        return True
    now = datetime.now(timezone.utc)
    idx = pick_next(rows, now, category=category)
    if idx is None:
        cat_label = f"category={category} " if category else ""
        print(f"[{channel}] no pending row {cat_label}in {queue_path.name} ({len(rows)} total) — slot 穴開け")
        return True
    row = rows[idx]
    text = row["text"]
    print(f"--- [{channel}] picked id={row['id']} category={row.get('category')} subcategory={row.get('subcategory')} ---")
    print(text)
    print(f"--- ({len(text)} chars) ---")
    if dry_run:
        print(f"[{channel}] DRY RUN — not posting, not mutating queue")
        return True

    if channel == "x":
        ok, post_id = post_x_via_buffer(text, live=live)
    elif channel == "threads":
        ok, post_id = post_threads_top_level(text)
    else:
        print(f"Unsupported channel: {channel}", file=sys.stderr)
        return False

    if not ok:
        return False
    row["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row["post_id"] = post_id
    rows[idx] = row
    _write_jsonl(queue_path, rows)
    print(f"[{channel}] queue updated: {row['id']} -> published_at={row['published_at']}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels", default="x,threads",
                        help="comma-separated channels: x / threads (default both)")
    parser.add_argument("--category", default=None, choices=[None, "news", "daily", ""],
                        help="restrict pick to this category. '' or omitted = no restriction (legacy).")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview only, no API call, no queue mutation")
    parser.add_argument("--live", action="store_true",
                        help="Buffer X: addToQueue (default: saveToDraft). Threads: 常に live")
    args = parser.parse_args()

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    category = args.category if args.category else None
    if category:
        print(f"[slot] category filter = {category}")
    ok = True
    if "x" in channels:
        ok &= consume_one(QUEUE_X, "x", args.dry_run, args.live, category=category)
    if "threads" in channels:
        ok &= consume_one(QUEUE_THREADS, "threads", args.dry_run, args.live, category=category)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
