#!/usr/bin/env python3
"""SNS に届いた返信・メンションを検出して inbox に貯める (返信取りこぼし対策)。

投稿レーン (notify_buffer / notify_threads / notify_bluesky) の裏返し。
届いた反応に気付けず放置になるのを潰す。

チャネル別の実測 (2026-09-05, run 33966085054 の probe):
  threads : 現行 THREADS_ACCESS_TOKEN のまま GET /{media-id}/replies が 200。
            再認可不要
  bluesky : app.bsky.notification.listNotifications が 200。reply/mention/quote
            を拾える
  x       : **Buffer からは取れない**。Buffer GraphQL の Query root は 13 個
            (account/channel/post/contentItem/idea/postTemplate/metrics/limits)
            しかなく、reply/comment/mention/conversation は型ごと存在しない。
            X 公式 API (GET /2/users/{id}/mentions) が唯一の経路で、2026-02 の
            従量課金移行後は自分のメンション取得が owned read ($0.001/件) 扱い。
            X_BEARER_TOKEN + X_USER_ID が設定されたときだけ有効化する

🚨 出力 — 第三者の本文とハンドルを扱う。**public リポジトリにコミットしない**。
   保存先は sns_inbox_store.inbox_dir() (SNS_INBOX_DIR) で、既定も repo の
   tmp/ を向けてある。標準出力に本文を出すのは --print-digest を明示した
   ときだけ (private リポジトリの Issue へ流す用途を想定)。

使い方:
    python scripts/fetch_sns_replies.py
    python scripts/fetch_sns_replies.py --channels threads,bluesky --lookback-days 14
    python scripts/fetch_sns_replies.py --dry-run          # store を触らない
    python scripts/fetch_sns_replies.py --print-digest     # 新着を Markdown で出す

env:
    SNS_INBOX_DIR          inbox の置き場所 (必須ではないが CI では明示する)
    THREADS_ACCESS_TOKEN / THREADS_USER_ID
    BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD / BLUESKY_PDS
    X_BEARER_TOKEN / X_USER_ID   (未設定なら x は skip)

exit code:
    0 = 走り切った (新着 0 件も成功)
    1 = 有効化されたチャネルが全滅した (secret 失効の黙殺を防ぐ)
    2 = 有効なチャネルが 1 つも無い
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import sns_inbox_store as store  # noqa: E402

THREADS_BASE = "https://graph.threads.net/v1.0"
DEFAULT_BLUESKY_PDS = "https://bsky.social"
X_BASE = "https://api.x.com/2"
TIMEOUT = 30

DEFAULT_LOOKBACK_DAYS = 14
# 1 run で見に行く自投稿の数。Threads は 1 投稿につき 1 リクエスト増えるので、
# lookback と併せて上限を持たせる。
MAX_OWN_POSTS = 25
MAX_REPLIES_PER_POST = 50
MAX_BLUESKY_NOTIFICATIONS = 100

BLUESKY_INTERESTING = {"reply": "reply", "mention": "mention", "quote": "quote"}


class ChannelError(RuntimeError):
    """そのチャネルが今回は取れなかった (他チャネルは続行する)。"""


def _request(url: str, *, headers: dict | None = None, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data else "GET",
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = (e.read().decode("utf-8", "replace") if e.fp else "")[:200]
        raise ChannelError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ChannelError(f"network error: {e.reason}") from e
    try:
        parsed = json.loads(body)
    except ValueError as e:
        raise ChannelError("response is not JSON") from e
    if not isinstance(parsed, dict):
        raise ChannelError("response is not a JSON object")
    return parsed


def _cutoff(lookback_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=lookback_days)


def _parse_iso(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def fetch_threads(lookback_days: int) -> list[dict]:
    token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    user_id = (os.environ.get("THREADS_USER_ID") or "").strip()
    if not token or not user_id:
        raise ChannelError("THREADS_ACCESS_TOKEN / THREADS_USER_ID 未設定")

    me = _request(f"{THREADS_BASE}/me?" + urllib.parse.urlencode(
        {"fields": "id,username", "access_token": token},
    ))
    own_username = str(me.get("username") or "")

    own = _request(f"{THREADS_BASE}/{user_id}/threads?" + urllib.parse.urlencode({
        "fields": "id,permalink,timestamp",
        "limit": str(MAX_OWN_POSTS),
        "access_token": token,
    }))

    cutoff = _cutoff(lookback_days)
    out: list[dict] = []
    for post in own.get("data") or []:
        if not isinstance(post, dict) or not post.get("id"):
            continue
        ts = _parse_iso(str(post.get("timestamp") or ""))
        if ts and ts < cutoff:
            continue
        out.extend(
            _fetch_thread_replies(str(post["id"]), token, own_username, cutoff),
        )
    return out


def _fetch_thread_replies(
    media_id: str, token: str, own_username: str, cutoff: datetime,
) -> list[dict]:
    try:
        payload = _request(f"{THREADS_BASE}/{media_id}/replies?" + urllib.parse.urlencode({
            "fields": "id,text,username,permalink,timestamp",
            "limit": str(MAX_REPLIES_PER_POST),
            "access_token": token,
        }))
    except ChannelError as e:
        # 1 投稿分の失敗で全体を落とさない (削除済み投稿など)。
        print(f"  [threads] {media_id} の replies 取得に失敗: {e}", file=sys.stderr)
        return []

    out: list[dict] = []
    for r in payload.get("data") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        username = str(r.get("username") or "")
        if own_username and username == own_username:
            continue  # 自分の投稿は返信対象ではない
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        ts = _parse_iso(str(r.get("timestamp") or ""))
        if ts and ts < cutoff:
            continue
        out.append(store.new_record(
            channel="threads",
            kind="reply",
            native_id=str(r["id"]),
            text=text,
            author=username,
            parent_id=media_id,
            permalink=str(r.get("permalink") or ""),
            created_at=str(r.get("timestamp") or ""),
        ))
    return out


# --------------------------------------------------------------------------
# Bluesky
# --------------------------------------------------------------------------

def fetch_bluesky(lookback_days: int) -> list[dict]:
    ident = (
        os.environ.get("BLUESKY_IDENTIFIER") or os.environ.get("BLUESKY_HANDLE") or ""
    ).strip()
    pw = (os.environ.get("BLUESKY_APP_PASSWORD") or "").strip()
    if not ident or not pw:
        raise ChannelError("BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD 未設定")

    pds = (os.environ.get("BLUESKY_PDS") or DEFAULT_BLUESKY_PDS).rstrip("/")
    session = _request(
        f"{pds}/xrpc/com.atproto.server.createSession",
        payload={"identifier": ident, "password": pw},
    )
    jwt = session.get("accessJwt")
    if not isinstance(jwt, str) or not jwt:
        raise ChannelError("createSession に accessJwt が無い")

    payload = _request(
        f"{pds}/xrpc/app.bsky.notification.listNotifications?limit={MAX_BLUESKY_NOTIFICATIONS}",
        headers={"Authorization": f"Bearer {jwt}"},
    )

    # updateSeen は **呼ばない**。既読を勝手に消すと K さん自身のアプリ側の
    # 未読表示が壊れる。重複は inbox の id で防いでいるので既読操作は不要。
    cutoff = _cutoff(lookback_days)
    out: list[dict] = []
    for n in payload.get("notifications") or []:
        if not isinstance(n, dict):
            continue
        kind = BLUESKY_INTERESTING.get(str(n.get("reason")))
        if not kind:
            continue
        rec = n.get("record") if isinstance(n.get("record"), dict) else {}
        text = str(rec.get("text") or "").strip()
        if not text:
            continue
        created = str(rec.get("createdAt") or n.get("indexedAt") or "")
        ts = _parse_iso(created)
        if ts and ts < cutoff:
            continue
        uri = str(n.get("uri") or "")
        author = n.get("author") if isinstance(n.get("author"), dict) else {}
        handle = str(author.get("handle") or "")
        out.append(store.new_record(
            channel="bluesky",
            kind=kind,
            native_id=uri,
            text=text,
            author=handle,
            parent_id=_bluesky_parent(rec),
            permalink=_bluesky_permalink(handle, uri),
            created_at=created,
        ))
    return out


def _bluesky_parent(record: dict) -> str:
    reply = record.get("reply")
    if not isinstance(reply, dict):
        return ""
    parent = reply.get("parent")
    if isinstance(parent, dict) and isinstance(parent.get("uri"), str):
        return parent["uri"]
    return ""


def _bluesky_permalink(handle: str, uri: str) -> str:
    """at://did/app.bsky.feed.post/<rkey> を bsky.app の URL に直す。"""
    if not handle or not uri.startswith("at://"):
        return ""
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""


# --------------------------------------------------------------------------
# X (secret があるときだけ)
# --------------------------------------------------------------------------

def fetch_x(lookback_days: int) -> list[dict]:
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip()
    user_id = (os.environ.get("X_USER_ID") or "").strip()
    if not token or not user_id:
        raise ChannelError(
            "X_BEARER_TOKEN / X_USER_ID 未設定 — X の返信は Buffer からは取れない "
            "(Buffer GraphQL に reply/mention/comment の型が無い)。X 公式 API の "
            "従量課金 (owned read $0.001/件) を有効化した場合のみ動く",
        )

    start = _cutoff(lookback_days).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _request(
        f"{X_BASE}/users/{user_id}/mentions?" + urllib.parse.urlencode({
            "max_results": "50",
            "start_time": start,
            "tweet.fields": "created_at,conversation_id,in_reply_to_user_id,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username",
        }),
        headers={"Authorization": f"Bearer {token}"},
    )

    users = {
        u.get("id"): u.get("username")
        for u in ((payload.get("includes") or {}).get("users") or [])
        if isinstance(u, dict)
    }
    out: list[dict] = []
    for t in payload.get("data") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        if str(t.get("author_id")) == user_id:
            continue
        handle = str(users.get(t.get("author_id")) or "")
        out.append(store.new_record(
            channel="x",
            kind="mention",
            native_id=str(t["id"]),
            text=str(t.get("text") or "").strip(),
            author=handle,
            parent_id=str(t.get("conversation_id") or ""),
            permalink=f"https://x.com/{handle}/status/{t['id']}" if handle else "",
            created_at=str(t.get("created_at") or ""),
        ))
    return out


FETCHERS = {"threads": fetch_threads, "bluesky": fetch_bluesky, "x": fetch_x}


# --------------------------------------------------------------------------

def render_digest(records: list[dict]) -> str:
    """新着を Markdown にする。private リポジトリの Issue へ流す前提の文面。"""
    if not records:
        return "新着の返信・メンションはありません。"
    lines = [f"未対応の返信・メンション **{len(records)} 件**", ""]
    for r in records:
        head = f"### {r['channel']} / {r['kind']}"
        if r.get("author"):
            head += f" — @{r['author']}"
        lines.append(head)
        lines.append("")
        lines.append(f"- 受信: {r.get('created_at') or '不明'}")
        if r.get("permalink"):
            lines.append(f"- 元投稿: {r['permalink']}")
        lines.append(f"- inbox id: `{r['id']}`")
        lines.append("")
        lines.append("> " + r["text"].replace("\n", "\n> "))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channels", default="threads,bluesky,x")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="store を書き換えない")
    ap.add_argument(
        "--print-digest", action="store_true",
        help="新着を Markdown で標準出力に出す (private な出力先でのみ使うこと)",
    )
    ap.add_argument("--digest-out", default="", help="digest の書き出し先ファイル")
    args = ap.parse_args(argv)

    requested = [c.strip() for c in args.channels.split(",") if c.strip()]
    unknown = [c for c in requested if c not in FETCHERS]
    if unknown:
        print(f"未知の channel: {', '.join(unknown)}", file=sys.stderr)
        return 2

    directory = store.inbox_dir()
    fetched: list[dict] = []
    ok, failed, skipped = [], [], []

    for name in requested:
        try:
            items = FETCHERS[name](args.lookback_days)
        except ChannelError as e:
            # secret 未設定は「設計上の skip」、それ以外は「失敗」。両者を混ぜると
            # secret 失効がずっと緑のまま黙殺される (#4793 と同じ形)。
            if "未設定" in str(e):
                skipped.append((name, str(e)))
            else:
                failed.append((name, str(e)))
            print(f"[{name}] skip/fail: {e}", file=sys.stderr)
            continue
        ok.append((name, len(items)))
        fetched.extend(items)

    if not ok and not failed:
        print("有効なチャネルが 1 つも無い", file=sys.stderr)
        return 2

    added = fetched if args.dry_run else store.record_new_items(fetched, directory)

    print(f"inbox: {directory}")
    for name, n in ok:
        print(f"  {name:8s} 取得 {n} 件")
    for name, why in skipped:
        print(f"  {name:8s} skip ({why})")
    for name, why in failed:
        print(f"  {name:8s} FAIL ({why})")
    print(f"新規 {len(added)} 件" + (" (dry-run: 保存していない)" if args.dry_run else ""))

    digest = render_digest(added)
    if args.digest_out:
        with open(args.digest_out, "w", encoding="utf-8") as f:
            f.write(digest)
    if args.print_digest:
        print("\n" + digest)

    # 有効化されたチャネルが全滅したら赤くする。緑のまま止まるレーンを作らない。
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
